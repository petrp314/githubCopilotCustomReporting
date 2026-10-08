"""Read-only enterprise collection plus the sole permitted POST: billing exports."""

from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import time

from .http import HttpClient, SourceError, parse_json, positive_limit
from .token_import import (
    TokenImportError, UnsupportedTokenSchema, read_tokens_bytes,
    validate_canonical_rows, validate_period,
)


def _now():
    return datetime.now(timezone.utc)


def _iso_now():
    return _now().isoformat().replace("+00:00", "Z")


def _days(start, end):
    for offset in range((end - start).days + 1):
        yield (start + timedelta(days=offset)).isoformat()


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise SourceError("Source returned an invalid identifier.")
    return value


def _objects(value):
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise SourceError("Source returned an unsupported schema.")
    return value


def _object(value):
    if not isinstance(value, dict):
        raise SourceError("Source returned an unsupported schema.")
    return value


def _repository_roots():
    roots = set()
    for location in (Path.cwd(), Path(__file__).resolve().parent):
        for parent in (location, *location.parents):
            if (parent / ".git").exists():
                roots.add(parent.resolve())
    return roots


def _prepare_state_directory(state_dir):
    path = Path(state_dir).expanduser().resolve()
    if any(path == root or root in path.parents for root in _repository_roots()):
        raise ValueError("Collector state must be outside Git repositories.")
    if any((parent / ".git").exists() for parent in (path, *path.parents)):
        raise ValueError("Collector state must be outside Git repositories.")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    exports = path / "exports"
    if exports.is_symlink():
        raise ValueError("Collector state must not use symbolic links.")
    exports.mkdir(mode=0o700, exist_ok=True)
    info = exports.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("Collector export state must be private.")
    return exports


class Collector:
    def __init__(self, config: dict, state_dir: Path):
        if not isinstance(config, dict):
            raise ValueError("Invalid collector configuration.")
        enterprise = config.get("enterprise")
        if (not isinstance(enterprise, str)
                or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,98}[A-Za-z0-9])?",
                                    enterprise)):
            raise ValueError("Invalid enterprise slug.")
        self.enterprise = enterprise
        self.prefix = "/enterprises/" + enterprise
        self.max_rows = positive_limit(config.get("max_rows"), 200000, 2000000)
        self.max_pages = positive_limit(config.get("max_pages"), 1000, 10000)
        self.poll_attempts = positive_limit(config.get("poll_attempts"), 3, 20)
        self.token_env = config.get("token_env", "COPILOT_REPORTING_TOKEN")
        self.seat_token_env = config.get("seat_token_env") or self.token_env
        for name in (self.token_env, self.seat_token_env):
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise ValueError("Invalid credential environment variable.")
        if any(key in config for key in ("token", "seat_token", "authorization")):
            raise ValueError("Credentials must only come from the environment.")
        self.http = HttpClient(
            api_origin=config.get("api_origin", "https://api.github.com"),
            api_version=config.get("api_version", "2026-03-10"),
            download_hosts=config.get("download_hosts", []),
            max_response_bytes=config.get("max_response_bytes", 16 * 1024 * 1024),
            max_attempts=config.get("max_attempts", 3),
        )
        self.state_dir = _prepare_state_directory(state_dir)

    def _api(self, suffix, *, query=None, method="GET", payload=None, seats=False):
        token = os.environ.get(self.seat_token_env if seats else self.token_env)
        return self.http.api(
            self.prefix + suffix, token, query=query, method=method, payload=payload
        )

    def _bounded(self, rows):
        if len(rows) > self.max_rows:
            raise SourceError("Source exceeds the row limit.")
        return rows

    def collect(self, start_day: str, end_day: str) -> dict:
        start, end = validate_period(start_day, end_day)
        if (end - start).days >= 366:
            raise ValueError("Collection periods may not exceed 366 days.")
        snapshot = {
            "schema_version": 1, "enterprise": self.enterprise,
            "collected_at": _iso_now(), "start_day": start_day, "end_day": end_day,
            "sources": {},
        }
        operations = (
            ("seats", self._seats),
            ("cost_centers", self._cost_centers),
            ("usage", lambda: self._usage(start, end, "users")),
            ("enterprise_usage", lambda: self._usage(start, end, "enterprise")),
            ("billing", lambda: self._billing(start, end)),
            ("tokens", lambda: self._tokens(start, end)),
        )
        for name, operation in operations:
            source = {"collected_at": _iso_now(), "scope": "enterprise"}
            if name in ("usage", "enterprise_usage"):
                source["missing_days"] = []
            try:
                source["data"] = operation()
                source["status"] = "ok"
            except UnsupportedTokenSchema:
                source.update(
                    status="unavailable",
                    reason="Token report schema is unsupported; use an approved CSV import.",
                )
            except TokenImportError:
                source.update(status="error", reason="Token report validation failed.")
            except SourceError as error:
                source.update(
                    status="unavailable" if error.unavailable else "error",
                    reason=str(error),
                )
                if name in ("usage", "enterprise_usage"):
                    source["missing_days"] = error.missing_days or list(_days(start, end))
            except (OSError, ValueError, TypeError, KeyError, RecursionError):
                source.update(status="error", reason="Source collection failed safely.")
                if name in ("usage", "enterprise_usage"):
                    source["missing_days"] = list(_days(start, end))
            snapshot["sources"][name] = source
        return snapshot

    def _seats(self):
        grants, fingerprints = [], set()
        total_seats = None
        for page in range(1, self.max_pages + 1):
            response = self._api(
                "/copilot/billing/seats", query={"per_page": 100, "page": page}, seats=True
            )
            payload = _object(response.json())
            total = payload.get("total_seats")
            if type(total) is not int or total < 0:
                raise SourceError("Source returned an unsupported seat schema.")
            if total_seats is None:
                total_seats = total
            elif total != total_seats:
                raise SourceError("Seat roster changed during pagination; retry later.")
            rows = _objects(payload.get("seats"))
            fingerprint = hashlib.sha256(
                json.dumps(rows, sort_keys=True).encode()
            ).digest()
            if rows and fingerprint in fingerprints:
                raise SourceError("Source repeated a pagination page.")
            fingerprints.add(fingerprint)
            grants.extend(rows)
            self._bounded(grants)
            link = response.headers.get("link", "")
            next_page = bool(re.search(r'rel=["\']?next\b', link))
            if (link and not next_page) or (not next_page and len(rows) < 100):
                return {"total_seats": total_seats, "seats": grants}
            if next_page and not rows:
                raise SourceError("Source returned invalid pagination.")
        raise SourceError("Seat pagination exceeds the page limit.")

    def _cost_centers(self):
        listed = []
        for state in ("active", "deleted"):
            payload = _object(self._api(
                "/settings/billing/cost-centers", query={"state": state}
            ).json())
            listed.extend(_objects(payload.get("costCenters")))
            self._bounded(listed)
        centers, seen = [], set()
        resource_count = 0
        for item in listed:
            center_id = _identifier(item.get("id"))
            if center_id in seen:
                raise SourceError("Source returned duplicate cost centers.")
            seen.add(center_id)
            center, resources, resource_keys = None, [], set()
            for page in range(1, self.max_pages + 1):
                payload = _object(self._api(
                    "/settings/billing/cost-centers/" + center_id,
                    query={"page": page, "per_page": 100},
                ).json())
                if payload.get("id") != center_id:
                    raise SourceError("Cost center identity does not match the request.")
                if center is None:
                    center = dict(payload)
                elif any(payload.get(key) != center.get(key) for key in ("name", "state")):
                    raise SourceError("Cost center changed during pagination; retry later.")
                rows = _objects(payload.get("resources"))
                for row in rows:
                    resource_key = json.dumps(row, sort_keys=True)
                    if resource_key in resource_keys:
                        raise SourceError("Source returned duplicate cost center resources.")
                    resource_keys.add(resource_key)
                resources.extend(rows)
                resource_count += len(rows)
                if resource_count > self.max_rows:
                    raise SourceError("Cost center resources exceed the row limit.")
                has_next = payload.get("has_next_page", False)
                if type(has_next) is not bool:
                    raise SourceError("Source returned invalid pagination.")
                if not has_next:
                    center["resources"] = resources
                    center.pop("has_next_page", None)
                    centers.append(center)
                    break
                if not rows:
                    raise SourceError("Source returned invalid pagination.")
            else:
                raise SourceError("Cost center pagination exceeds the page limit.")
        return centers

    def _links(self, value):
        if (not isinstance(value, list) or not value
                or len(value) > self.max_pages
                or any(not isinstance(link, str) for link in value)
                or len(set(value)) != len(value)):
            raise SourceError("Source returned invalid report parts.")
        return value

    def _usage_day(self, day, kind):
        payload = _object(self._api(
            "/copilot/metrics/reports/" + kind + "-1-day", query={"day": day}
        ).json())
        if payload.get("report_day") != day:
            raise SourceError("Usage report period does not match the request.")
        rows, seen = [], set()
        for link in self._links(payload.get("download_links")):
            data = self.http.download(link)
            try:
                lines = data.decode("utf-8-sig").splitlines()
            except UnicodeError:
                raise SourceError("Source report encoding is invalid.") from None
            for line in lines:
                if not line.strip():
                    continue
                row = _object(parse_json(line))
                if row.get("day") != day:
                    raise SourceError("Usage row period does not match the request.")
                identity = (day, str(row["user_id"])) if (
                    kind == "users" and row.get("user_id") is not None
                ) else json.dumps(row, sort_keys=True)
                if identity in seen:
                    raise SourceError("Source returned duplicate usage rows.")
                seen.add(identity)
                rows.append(row)
                self._bounded(rows)
        return rows

    def _usage(self, start, end, kind):
        rows, missing, failures = [], [], []
        for day in _days(start, end):
            try:
                daily_rows = self._usage_day(day, kind)
            except SourceError as error:
                missing.append(day)
                failures.append(error)
                continue
            if len(rows) + len(daily_rows) > self.max_rows:
                raise SourceError("Source exceeds the row limit.")
            rows.extend(daily_rows)
        if failures:
            raise SourceError(
                "Daily usage reports are incomplete.",
                unavailable=all(error.unavailable for error in failures),
                missing_days=missing,
            )
        return rows

    def _billing(self, start, end):
        rows = []
        for day in _days(start, end):
            parsed_day = date.fromisoformat(day)
            period = {"year": parsed_day.year, "month": parsed_day.month, "day": parsed_day.day}
            payload = _object(self._api(
                "/settings/billing/ai_credit/usage", query=period
            ).json())
            if payload.get("enterprise") != self.enterprise or payload.get("timePeriod") != period:
                raise SourceError("Billing report scope does not match the request.")
            for item in _objects(payload.get("usageItems")):
                if any(item.get(key, day) != day for key in ("day", "date")):
                    raise SourceError("Billing row period does not match the request.")
                rows.append({**item, "day": day, "date": day, "timePeriod": period})
                self._bounded(rows)
        return rows

    def _tokens(self, start, end):
        rows = []
        window_start = start
        while window_start <= end:
            window_end = min(end, window_start + timedelta(days=30))
            rows.extend(self._export(window_start.isoformat(), window_end.isoformat()))
            self._bounded(rows)
            window_start = window_end + timedelta(days=1)
        return validate_canonical_rows(
            rows, start.isoformat(), end.isoformat(), self.max_rows
        )

    @contextmanager
    def _state_lock(self, key):
        path = self.state_dir / (key + ".lock")
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            if stat.S_IMODE(os.fstat(fd).st_mode) & 0o077:
                raise SourceError("Export state permissions are unsafe.")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SourceError("Export collection is already in progress.", unavailable=True) from None
            yield
        finally:
            os.close(fd)

    def _read_state(self, key):
        path = self.state_dir / (key + ".json")
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, "rb") as source:
            if stat.S_IMODE(os.fstat(source.fileno()).st_mode) & 0o077:
                raise SourceError("Export state permissions are unsafe.")
            data = source.read(self.http.max_response_bytes + 1)
        if len(data) > self.http.max_response_bytes:
            raise SourceError("Export state exceeds the byte limit.")
        state = _object(parse_json(data))
        if (state.get("schema_version") != 1 or type(state.get("expires_at")) is not int
                or type(state.get("created_at")) is not int):
            raise SourceError("Export state is invalid.")
        if state["expires_at"] <= int(time.time()):
            path.unlink()
            return None
        return state

    def _write_state(self, key, state):
        data = json.dumps(state, allow_nan=False, separators=(",", ":")).encode()
        if len(data) > self.http.max_response_bytes:
            raise SourceError("Export state exceeds the byte limit.")
        destination = self.state_dir / (key + ".json")
        staging = self.state_dir / (key + "." + secrets.token_hex(8) + ".part")
        try:
            fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as target:
                target.write(data)
                target.flush()
                os.fsync(target.fileno())
            os.replace(staging, destination)
            directory_fd = os.open(self.state_dir, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if staging.exists():
                staging.unlink()

    def _validate_job(self, job, start, end):
        job = _object(job)
        _identifier(job.get("id"))
        if (job.get("report_type") != "ai_credit" or job.get("start_date") != start
                or job.get("end_date") != end
                or job.get("status") not in ("processing", "completed", "failed")):
            raise SourceError("Export scope or schema does not match the request.")
        return job

    def _recover_submission(self, state, start, end):
        # A lost POST response is ambiguous. Reconcile, never blindly POST again.
        payload = _object(self._api("/settings/billing/reports").json())
        candidates = []
        for job in self._bounded(_objects(payload.get("usage_report_exports"))):
            if (job.get("report_type") == "ai_credit" and job.get("start_date") == start
                    and job.get("end_date") == end):
                try:
                    created = datetime.fromisoformat(job["created_at"].replace("Z", "+00:00"))
                    if created.tzinfo is None or created.timestamp() < state["created_at"] - 60:
                        continue
                except (KeyError, ValueError, AttributeError, TypeError):
                    continue
                candidates.append((created.timestamp(), job))
        if not candidates:
            raise SourceError(
                "Export submission is unresolved; retry after the state expires.",
                unavailable=True,
            )
        return self._validate_job(max(candidates, key=lambda item: item[0])[1], start, end)

    def _export(self, start, end):
        if not os.environ.get(self.token_env):
            raise SourceError("Credential is unavailable.", unavailable=True)
        key = hashlib.sha256(
            "\n".join((self.http.api_origin, self.enterprise, start, end)).encode()
        ).hexdigest()
        with self._state_lock(key):
            state = self._read_state(key)
            if state and state.get("status") == "completed":
                return validate_canonical_rows(state.get("rows"), start, end, self.max_rows)
            if state and state.get("status") == "failed":
                raise SourceError("Export failed; retry after the state expires.", unavailable=True)
            if state is None:
                timestamp = int(time.time())
                state = {
                    "schema_version": 1, "status": "submitting",
                    "created_at": timestamp, "expires_at": timestamp + 86400,
                }
                self._write_state(key, state)
                job = self._validate_job(self._api(
                    "/settings/billing/reports", method="POST",
                    payload={"report_type": "ai_credit", "start_date": start,
                             "end_date": end, "send_email": False},
                ).json(), start, end)
            elif state.get("status") == "submitting":
                job = self._recover_submission(state, start, end)
            elif state.get("status") == "processing":
                report_id = _identifier(state.get("id"))
                job = {"id": report_id, "status": "processing"}
            else:
                raise SourceError("Export state is invalid.")
            state.update(id=job["id"], status="processing")
            # Keep only job identity, never expiring signed links or API payloads.
            self._write_state(key, state)
            for attempt in range(self.poll_attempts + 1):
                if job["status"] == "failed":
                    state.update(status="failed", expires_at=int(time.time()) + 86400)
                    self._write_state(key, state)
                    raise SourceError("Report export failed.", unavailable=True)
                if job["status"] == "completed":
                    rows = []
                    for link in self._links(job.get("download_urls")):
                        rows.extend(read_tokens_bytes(
                            self.http.download(link), start, end,
                            max_bytes=self.http.max_response_bytes, max_rows=self.max_rows,
                        ))
                        self._bounded(rows)
                    rows = validate_canonical_rows(rows, start, end, self.max_rows)
                    # A day's cache prevents duplicate exports during retries while
                    # allowing provisional billing periods to refresh tomorrow.
                    state.update(status="completed", rows=rows,
                                 expires_at=int(time.time()) + 86400)
                    self._write_state(key, state)
                    return rows
                if attempt == self.poll_attempts:
                    break
                if attempt:
                    time.sleep(min(2 ** (attempt - 1), 15))
                job = self._validate_job(self._api(
                    "/settings/billing/reports/" + state["id"]
                ).json(), start, end)
                if job["id"] != state["id"]:
                    raise SourceError("Export identity does not match the request.")
            raise SourceError("Report export is still processing.", unavailable=True)
