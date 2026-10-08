"""Strict CSV ingestion for the documented AI billing report, not telemetry."""

import csv
from datetime import date
import io
import os
from pathlib import Path
import re
import stat

from .http import positive_limit


TOKEN_COLUMNS = ("input", "output", "cache_read", "cache_write")
IDENTITY_COLUMNS = ("user_id", "username", "cost_center_name", "cost_center_id")
CANONICAL_COLUMNS = ("day", "model") + IDENTITY_COLUMNS + TOKEN_COLUMNS
_IGNORED_COLUMNS = {
    "product", "sku", "quantity", "unit_type", "applied_cost_per_quantity",
    "gross_amount", "discount_amount", "net_amount", "organization",
    "repository", "workflow_path",
}
_ALIASES = {"date": "day"}


class TokenImportError(ValueError):
    """No offending header, cell, path, or employee data appears in the message."""


class UnsupportedTokenSchema(TokenImportError):
    pass


def validate_period(start_day, end_day):
    try:
        if any(not isinstance(value, str) or not re.fullmatch(
                r"\d{4}-\d{2}-\d{2}", value) for value in (start_day, end_day)):
            raise ValueError
        start, end = date.fromisoformat(start_day), date.fromisoformat(end_day)
        if start > end:
            raise ValueError
        return start, end
    except ValueError:
        raise TokenImportError("Invalid report date range.") from None


def _text(value, *, required=False):
    if value is None or value == "":
        if required:
            raise TokenImportError("Required report value is missing.")
        return None
    if (not isinstance(value, str) or len(value) > 1024 or value != value.strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise TokenImportError("Invalid report text value.")
    return value


def _count(value):
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,128}", value):
        raise TokenImportError("Token counts must be nonnegative integers.")
    return value.lstrip("0") or "0"


def token_row_key(row):
    """Report grain is day/model/user/cost-center; duplicate grains are rejected."""
    return tuple(row.get(key) for key in ("day", "model") + IDENTITY_COLUMNS)


def validate_canonical_rows(rows, start_day, end_day, max_rows=200000):
    start, end = validate_period(start_day, end_day)
    if not isinstance(rows, list) or len(rows) > max_rows:
        raise TokenImportError("Token report exceeds the row limit.")
    seen = set()
    result = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != set(CANONICAL_COLUMNS):
            raise TokenImportError("Invalid canonical token row.")
        day = _text(row["day"], required=True)
        try:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                raise ValueError
            if not start <= date.fromisoformat(day) <= end:
                raise ValueError
        except ValueError:
            raise TokenImportError("Report row is outside the requested period.") from None
        canonical = {"day": day, "model": _text(row["model"], required=True)}
        for key in IDENTITY_COLUMNS:
            value = row[key]
            if key == "user_id" and type(value) is int and value >= 0:
                value = str(value)
            canonical[key] = _text(value)
        for key in TOKEN_COLUMNS:
            canonical[key] = _count(row[key])
        if all(canonical[key] is None for key in TOKEN_COLUMNS):
            raise UnsupportedTokenSchema("Report has no usable token columns.")
        grain = token_row_key(canonical)
        if grain in seen:
            raise TokenImportError("Duplicate token report rows.")
        seen.add(grain)
        result.append(canonical)
    return result


def read_tokens_bytes(data, start_day, end_day, *, max_bytes=16 * 1024 * 1024,
                      max_rows=200000):
    """Read UTF-8 CSV with an exact, case-sensitive schema.

    Required: ``day`` (the only alias is ``date``), ``model``, and at least one
    of ``input``, ``output``, ``cache_read``, ``cache_write``. Optional identity
    fields: ``user_id``, ``username``, ``cost_center_name``, ``cost_center_id``.
    Missing/empty token cells become None, never zero. Counts are integer text.
    The documented billing columns product, sku, quantity, unit_type,
    applied_cost_per_quantity, gross_amount, discount_amount, net_amount,
    organization, repository, workflow_path are accepted but not interpreted.
    Unknown headers, aliases colliding with canonical names, extra/missing cells,
    duplicate day/model/user/cost-center grains, and out-of-period rows fail the
    entire import. Header-only reports are valid only with token column headers.
    """
    max_bytes = positive_limit(max_bytes, 16 * 1024 * 1024, 256 * 1024 * 1024)
    max_rows = positive_limit(max_rows, 200000, 2000000)
    validate_period(start_day, end_day)
    if not isinstance(data, bytes) or len(data) > max_bytes:
        raise TokenImportError("Token report exceeds the byte limit.")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeError:
        raise TokenImportError("Token report must be UTF-8 CSV.") from None
    if "\x00" in text:
        raise TokenImportError("Invalid token report encoding.")
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    try:
        raw_headers = next(reader)
        headers = [_ALIASES.get(header, header) for header in raw_headers]
        if (not headers or len(set(headers)) != len(headers)
                or any(header not in set(CANONICAL_COLUMNS) | _IGNORED_COLUMNS
                       for header in headers)):
            raise TokenImportError("Invalid or duplicate report headers.")
        if not {"day", "model"} <= set(headers):
            raise UnsupportedTokenSchema("Required token report columns are unavailable.")
        if not set(TOKEN_COLUMNS).intersection(headers):
            raise UnsupportedTokenSchema("Report has no usable token columns.")
        rows = []
        for cells in reader:
            if len(cells) != len(headers):
                raise TokenImportError("Malformed token report row.")
            if len(rows) >= max_rows:
                raise TokenImportError("Token report exceeds the row limit.")
            raw = dict(zip(headers, cells))
            rows.append({key: raw.get(key) for key in CANONICAL_COLUMNS})
    except (csv.Error, StopIteration):
        raise TokenImportError("Malformed token report CSV.") from None
    return validate_canonical_rows(rows, start_day, end_day, max_rows)


def read_tokens(path, start_day, end_day, *, max_bytes=16 * 1024 * 1024,
                max_rows=200000):
    """Read a protected UTF-8 CSV; headers are case-sensitive.

    Require day (alias: date), model, and at least one of input/output/cache_read/
    cache_write. Optional user_id/username/cost_center_name/cost_center_id are
    preserved. See read_tokens_bytes for the accepted ignored billing columns.
    No other aliases are accepted. Missing token cells remain None.
    """
    max_bytes = positive_limit(max_bytes, 16 * 1024 * 1024, 256 * 1024 * 1024)
    try:
        fd = os.open(Path(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise TokenImportError("Token report must be a regular file.")
            if info.st_size > max_bytes:
                raise TokenImportError("Token report exceeds the byte limit.")
            data = source.read(max_bytes + 1)
    except OSError:
        raise TokenImportError("Token report could not be read.") from None
    return read_tokens_bytes(
        data, start_day, end_day, max_bytes=max_bytes, max_rows=max_rows
    )
