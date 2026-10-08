"""Strict source-grain validation; no conversion between billing and telemetry."""

import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext


SOURCES = (
    "seats", "cost_centers", "usage", "enterprise_usage",
    "billing", "billing_centers", "tokens",
)
TOKENS = ("input", "output", "cache_read", "cache_write")


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def day(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("Expected a UTC calendar date")
    return date.fromisoformat(value).isoformat()


def days(start, end):
    first, last = date.fromisoformat(day(start)), date.fromisoformat(day(end))
    if first > last or (last - first).days > 394:
        raise ValueError("Reporting range must contain 1 to 395 days")
    return [(first + timedelta(days=i)).isoformat() for i in range((last-first).days + 1)]


def text(value, default="Unknown"):
    if value is None or value == "":
        return default
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise ValueError("Invalid dimension")
    value = str(value)
    if len(value) > 256 or any(ord(c) < 32 for c in value):
        raise ValueError("Invalid dimension")
    return value


def number(value, integer=False, nullable=True):
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("Invalid numeric metric")
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("Invalid numeric metric") from None
    if not result.is_finite() or result < 0 or result.adjusted() > 24 or result.as_tuple().exponent < -12:
        raise ValueError("Numeric metric outside supported precision")
    if integer and result != result.to_integral_value():
        raise ValueError("Expected a nonnegative integer")
    if integer:
        return int(result)
    return format(result, "f")


def total(values):
    values = list(values)
    if not values or any(value is None for value in values):
        return None
    with localcontext() as context:
        context.prec = 64
        return format(sum((Decimal(str(v)) for v in values), Decimal(0)), "f")


def surface(row, name, field):
    value = row.get(name)
    if value is None:
        return None
    if not isinstance(value, dict) or not isinstance(value.get("token_usage"), dict):
        raise ValueError("Unsupported surface token schema")
    return number(value["token_usage"].get(field), integer=True)


def normalize(source, data):
    """Keep only permitted fields. Unknown fields never become published data."""
    if source == "seats":
        seats = data["seats"]
        result = []
        for row in seats:
            user = row.get("assignee") or {}
            result.append({
                "user_id": text(user["id"]) if user.get("id") is not None else None,
                "login": text(user.get("login")) if user.get("login") else None,
                "organization": text((row.get("assigning_organization") or {}).get("id")),
                "created_at": row.get("created_at"),
            })
        return {"total_seats": number(data["total_seats"], integer=True, nullable=False), "seats": result}
    if source == "cost_centers":
        return [
            {"id": text(row["id"]), "name": text(row["name"]), "state": text(row.get("state")),
             "resources": [{"type": text(resource["type"]), "name": text(resource["name"])}
                           for resource in row.get("resources", [])]}
            for row in data
        ]
    if not isinstance(data, list):
        raise ValueError("Expected source records")
    result, identities = [], set()
    for row in data:
        record = {"day": day(row.get("day", row.get("date", "")[:10]))}
        if source in ("usage", "enterprise_usage"):
            record["interactions"] = number(row.get("user_initiated_interaction_count"), integer=True)
            for prefix, field in (("cli", "totals_by_cli"), ("app", "totals_by_copilot_app")):
                record[prefix + "_prompt_tokens"] = surface(row, field, "prompt_tokens_sum")
                record[prefix + "_output_tokens"] = surface(row, field, "output_tokens_sum")
            if source == "enterprise_usage":
                record["active_users"] = number(row.get("daily_active_users"), integer=True, nullable=False)
                key = record["day"]
            else:
                record["user_id"] = text(row["user_id"])
                record["login"] = text(row.get("user_login")) if row.get("user_login") else None
                # A source per-user row is observable activity even with empty IDE arrays.
                record["models"] = [
                    {"model": text(item.get("model")), "interactions": number(
                        item.get("user_initiated_interaction_count"), integer=True, nullable=False)}
                    for item in row.get("totals_by_model_feature", [])
                ]
                key = (record["day"], record["user_id"])
        elif source in ("billing", "billing_centers"):
            for key, upstream in (
                ("gross_quantity", "grossQuantity"), ("discount_quantity", "discountQuantity"),
                ("net_quantity", "netQuantity"), ("gross_amount", "grossAmount"),
                ("discount_amount", "discountAmount"), ("net_amount", "netAmount"),
                ("price_per_unit", "pricePerUnit"),
            ):
                record[key] = number(row.get(upstream, row.get(key)), nullable=False)
            record.update({
                "model": text(row.get("model")), "unit": text(row.get("unitType", row.get("unit"))),
                "currency": text(row.get("currency")), "product": text(row.get("product")),
                "sku": text(row.get("sku")), "cost_center_id": text(row.get("cost_center_id"), "__enterprise__"),
                "cost_center_name": text(row.get("cost_center_name"), "Enterprise total"),
                "attribution": "source-billed", "source": source,
            })
            if record["unit"] == "Unknown" or "copilot" not in record["product"].lower():
                raise ValueError("Unsupported billing product or unit")
            key = tuple(record[k] for k in ("day", "model", "unit", "currency", "product", "sku", "cost_center_id"))
        elif source == "tokens":
            record.update({
                "model": text(row.get("model")), "user_id": text(row["user_id"]) if row.get("user_id") is not None else None,
                "login": text(row.get("username")) if row.get("username") else None,
                "cost_center_id": text(row.get("cost_center_id"), "unresolved"),
                "cost_center_name": text(row.get("cost_center_name"), "Unknown / unresolved"),
                "attribution": "source-billed" if row.get("cost_center_id") else "unknown",
                "source": "tokens",
            })
            for category in TOKENS:
                value = number(row.get(category), integer=True)
                record[category] = str(value) if value is not None else None
            if all(record[k] is None for k in TOKENS):
                raise ValueError("Token categories unavailable")
            key = tuple(record[k] for k in ("day", "model", "user_id", "login", "cost_center_id", "cost_center_name"))
        else:
            raise ValueError("Unsupported source")
        if key in identities:
            raise ValueError("Duplicate source grain")
        identities.add(key)
        result.append(record)
    return result
