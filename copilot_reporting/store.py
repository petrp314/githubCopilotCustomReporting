"""Protected SQLite snapshots, atomic source replacement and an ingestion ledger."""

import hashlib
import json
import os
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from .domain import SOURCES, day, days, normalize, utc_now


ROOT = Path(__file__).resolve().parent.parent


def protected_directory(path):
    path = Path(path).resolve()
    if path == ROOT or ROOT in path.parents:
        raise ValueError("Protected storage must be outside the repository")
    if any((parent / ".git").exists() for parent in (path, *path.parents)):
        raise ValueError("Protected storage must be outside Git working trees")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise ValueError("Protected storage must be owner-only (mode 0700)")
    return path


class Store:
    def __init__(self, path, enterprise, api_origin="https://api.github.com"):
        directory = protected_directory(path)
        db = directory / "reporting.sqlite"
        if db.is_symlink():
            raise ValueError("Storage symlinks are not permitted")
        self.connection = sqlite3.connect(db)
        os.chmod(db, 0o600)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript("""
            PRAGMA secure_delete=ON;
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS partitions (
                source TEXT, day TEXT, payload TEXT NOT NULL, collected_at TEXT NOT NULL,
                hash TEXT NOT NULL, PRIMARY KEY(source, day));
            CREATE TABLE IF NOT EXISTS status (
                source TEXT PRIMARY KEY, status TEXT NOT NULL, attempted_at TEXT NOT NULL,
                last_success TEXT, message TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS ledger (
                source TEXT, attempted_at TEXT, start_day TEXT, end_day TEXT,
                status TEXT, hash TEXT, row_count INTEGER);
            CREATE TABLE IF NOT EXISTS aggregates (
                start_day TEXT, end_day TEXT, payload TEXT NOT NULL, created_at TEXT,
                PRIMARY KEY(start_day, end_day));
        """)
        existing = self.connection.execute("SELECT value FROM metadata WHERE key='enterprise'").fetchone()
        if existing and existing["value"] != enterprise:
            self.connection.close()
            raise ValueError("Storage belongs to a different enterprise")
        existing_origin = self.connection.execute("SELECT value FROM metadata WHERE key='api_origin'").fetchone()
        if existing_origin and existing_origin["value"] != api_origin:
            self.connection.close()
            raise ValueError("Storage belongs to a different API origin")
        with self.connection:
            self.connection.execute("INSERT OR IGNORE INTO metadata VALUES ('enterprise', ?)", (enterprise,))
            self.connection.execute("INSERT OR IGNORE INTO metadata VALUES ('api_origin', ?)", (api_origin,))

    def close(self):
        self.connection.close()

    def ingest(self, snapshot):
        if snapshot.get("schema_version") != 1:
            raise ValueError("Unsupported snapshot version")
        enterprise = self.connection.execute("SELECT value FROM metadata WHERE key='enterprise'").fetchone()[0]
        if snapshot.get("enterprise") != enterprise:
            raise ValueError("Snapshot enterprise mismatch")
        if snapshot.get("api_version"):
            with self.connection:
                self.connection.execute("INSERT OR REPLACE INTO metadata VALUES ('api_version', ?)",
                                        (snapshot["api_version"],))
        start, end = snapshot["start_day"], snapshot["end_day"]
        period = days(start, end)
        collected_at = snapshot["collected_at"]
        snapshot_day = day(collected_at[:10])
        for source, item in snapshot["sources"].items():
            if source not in SOURCES:
                continue
            status = item.get("status")
            message, count, digest = "", 0, None
            partitioned = {}
            if status == "ok":
                try:
                    if item.get("missing_days"):
                        raise ValueError("Missing source days")
                    data = normalize(source, item["data"])
                    if source in ("seats", "cost_centers"):
                        partitioned = {snapshot_day: data}
                    else:
                        partitioned = {d: [] for d in period}
                        for row in data:
                            if row["day"] not in partitioned:
                                raise ValueError("Source record outside requested period")
                            partitioned[row["day"]].append(row)
                        if source == "enterprise_usage" and any(len(rows) != 1 for rows in partitioned.values()):
                            raise ValueError("Missing enterprise daily record")
                    encoded = json.dumps(data, sort_keys=True, separators=(",", ":"))
                    digest = hashlib.sha256(encoded.encode()).hexdigest()
                    count = len(data["seats"]) if source == "seats" else len(data)
                except (ValueError, KeyError, TypeError, AttributeError):
                    status, message = "error", "Schema or coverage validation failed; last good data retained."
            elif status == "unavailable":
                message = "Source unavailable; verify tenant capability and read permissions."
            else:
                status, message = "error", "Collection failed; verify source access and retry."
            if status != "ok":
                message = {
                    "access_denied": "Access denied; verify credential scope, endpoint permissions, and enterprise metrics policy.",
                    "unsupported_or_missing": "Source report unavailable or missing; verify entitlement and publication delay.",
                    "rate_limited": "Source rate limit reached; retry after the upstream reset.",
                    "unsupported_schema": "Token export schema unsupported; validate tenant columns or use an approved import.",
                    "invalid_import": "Token import failed schema, bounds, or period validation.",
                }.get(item.get("error_code"), message)
            with self.connection:
                if status == "ok":
                    for partition, data in partitioned.items():
                        payload = json.dumps(data, sort_keys=True, separators=(",", ":"))
                        self.connection.execute(
                            "INSERT OR REPLACE INTO partitions VALUES (?,?,?,?,?)",
                            (source, partition, payload, collected_at, hashlib.sha256(payload.encode()).hexdigest()),
                        )
                self.connection.execute(
                    """INSERT INTO status VALUES (?,?,?,?,?)
                    ON CONFLICT(source) DO UPDATE SET status=excluded.status,
                    attempted_at=excluded.attempted_at, message=excluded.message,
                    last_success=COALESCE(excluded.last_success,status.last_success)""",
                    (source, status, collected_at, collected_at if status == "ok" else None, message),
                )
                self.connection.execute("INSERT INTO ledger VALUES (?,?,?,?,?,?,?)",
                                        (source, collected_at, start, end, status, digest, count))

    def partitions(self, source, start, end):
        return {
            row["day"]: json.loads(row["payload"])
            for row in self.connection.execute(
                "SELECT day,payload FROM partitions WHERE source=? AND day BETWEEN ? AND ? ORDER BY day",
                (source, start, end))
        }

    def statuses(self):
        return {row["source"]: dict(row) for row in self.connection.execute("SELECT * FROM status")}

    def latest(self, source):
        row = self.connection.execute(
            "SELECT day,payload FROM partitions WHERE source=? ORDER BY day DESC LIMIT 1", (source,)).fetchone()
        return (row["day"], json.loads(row["payload"])) if row else (None, None)

    def save_aggregate(self, report):
        with self.connection:
            self.connection.execute("INSERT OR REPLACE INTO aggregates VALUES (?,?,?,?)",
                                    (report["period"]["start"], report["period"]["end"], json.dumps(report), utc_now()))

    def archived_aggregate(self, start, end):
        days(start, end)
        row = self.connection.execute(
            "SELECT payload FROM aggregates WHERE start_day=? AND end_day=?", (start, end)).fetchone()
        if not row:
            raise ValueError("No approved archive exists for this exact reporting window")
        return json.loads(row["payload"])

    def latest_aggregate(self):
        row = self.connection.execute("SELECT payload FROM aggregates ORDER BY end_day DESC LIMIT 1").fetchone()
        return json.loads(row["payload"]) if row else None

    def expire(self, raw_days, aggregate_days, today=None):
        today = today or date.today()
        raw_cutoff = (today - timedelta(days=raw_days - 1)).isoformat()
        aggregate_cutoff = (today - timedelta(days=aggregate_days - 1)).isoformat()
        with self.connection:
            self.connection.execute("DELETE FROM partitions WHERE day < ?", (raw_cutoff,))
            self.connection.execute("DELETE FROM ledger WHERE substr(attempted_at,1,10) < ?", (raw_cutoff,))
            self.connection.execute("DELETE FROM aggregates WHERE start_day < ?", (aggregate_cutoff,))
        self.connection.execute("VACUUM")
