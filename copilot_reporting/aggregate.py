"""Publish explicit aggregates only; retain unavailable and suppressed populations."""

from collections import defaultdict
import hashlib
import json

from .domain import SOURCES, TOKENS, days, total, utc_now


def policy_fingerprint(publication):
    return hashlib.sha256(json.dumps(publication, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def metric(value, unit, source, coverage, start, end):
    return {"value": value, "unit": unit, "source": source, "coverage": coverage, "window": f"{start} / {end} UTC"}


def identities(seats):
    rows = seats["seats"]
    users = {row["user_id"] for row in rows if row["user_id"] is not None}
    complete = all(row["user_id"] is not None for row in rows) and len(users) == seats["total_seats"]
    return users, complete


def billing_key(row):
    return tuple(row[k] for k in ("day", "model", "product", "sku", "unit", "currency"))


def reconcile(enterprise, centers):
    fields = ("gross_quantity", "discount_quantity", "net_quantity", "gross_amount", "discount_amount", "net_amount")
    groups = []
    for rows in (enterprise, centers):
        result = defaultdict(list)
        for row in rows:
            result[billing_key(row)].append(row)
        groups.append({key: tuple(total(r[field] for r in values) for field in fields)
                       for key, values in result.items()})
    # Decimal equality, not formatted-string equality (1.00 and 1 are equal).
    from decimal import Decimal
    return {k: tuple(Decimal(v) for v in values) for k, values in groups[0].items()} == {
        k: tuple(Decimal(v) for v in values) for k, values in groups[1].items()
    }


def build_report(store, start, end, publication, demo=False):
    if publication.get("approved") is not True or publication.get("audience") != "shared-aggregates":
        raise ValueError("Static publication requires explicit common-audience approval; scoped access needs a backend")
    threshold = publication.get("minimum_cohort", 5)
    if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold < 2:
        raise ValueError("Minimum cohort must be an approved integer of at least two")
    period = days(start, end)
    parts = {source: store.partitions(source, start, end) for source in SOURCES}
    flat = {source: [row for partition in parts[source].values() for row in partition]
            for source in SOURCES if source not in ("seats", "cost_centers")}
    statuses = store.statuses()
    sources = []
    for source in SOURCES:
        status = statuses.get(source, {})
        latest_day, _ = store.latest(source)
        missing = [d for d in period if d not in parts[source]]
        sources.append({
            "id": source, "label": source.replace("_", " ").title(),
            "status": status.get("status", "unavailable"),
            "last_successful_collection": status.get("last_success"),
            "latest_available_day": latest_day, "missing_days": missing,
            "message": status.get("message", "Not collected.") or (
                "Point-in-time snapshots; no retrospective license or membership inference."
                if source in ("seats", "cost_centers") else "Recent upstream days may remain provisional."),
        })
    usage_complete = all(d in parts["usage"] for d in period)
    seats_complete = all(d in parts["seats"] and identities(parts["seats"][d])[1] for d in period)
    users = {row["user_id"] for row in flat["usage"]}
    licensed, qualifying = set(), set()
    for d in period:
        if d in parts["seats"]:
            roster, _ = identities(parts["seats"][d])
            licensed.update(roster)
            qualifying.update(row["user_id"] for row in parts["usage"].get(d, []) if row["user_id"] in roster)
    coverage = "Complete daily source coverage" if usage_complete else "Incomplete daily source coverage"
    roster_coverage = "Daily point-in-time snapshots (intraday changes not represented)" if seats_complete else "Historical license coverage unavailable"
    licensed_count = len(licensed) if seats_complete else None
    active_count = len(users) if usage_complete else None
    adoption = round(100 * len(qualifying) / len(licensed), 2) if seats_complete and usage_complete and licensed else None
    # Suppress complementary populations as well as small positive cohorts.
    populations = [len(users), len(licensed), len(qualifying), len(licensed - qualifying)]
    hide_users = any(0 < n < threshold for n in populations)
    daily, models = [], []
    model_groups = defaultdict(list)
    for row in flat["usage"]:
        for model in row["models"]:
            model_groups[(row["day"], model["model"])].append((row["user_id"], model["interactions"]))
    hide_models = hide_users or any(len({user for user, _ in values}) < threshold for values in model_groups.values())
    hide_daily = hide_users
    for d in period:
        rows = parts["usage"].get(d)
        roster = parts["seats"].get(d)
        daily_users = len({row["user_id"] for row in rows}) if rows is not None else None
        seated, roster_valid = identities(roster) if roster else (set(), False)
        daily_qualifying = {row["user_id"] for row in rows or []} & seated
        if any(0 < n < threshold for n in (daily_users or 0, len(seated), len(seated - daily_qualifying))):
            hide_daily = True
        daily.append({
            "day": d, "active_users": daily_users,
            "licensed_users": len(seated) if roster_valid else None,
            "interactions": int(total(row["interactions"] for row in rows)) if rows and total(row["interactions"] for row in rows) is not None else (0 if rows == [] else None),
            **{field: total(row[field] for row in rows) if rows else None for field in (
                "cli_prompt_tokens", "cli_output_tokens", "app_prompt_tokens", "app_output_tokens")},
        })
    hide_models = hide_models or hide_daily
    if not hide_models:
        models = [{"day": d, "model": model, "active_users": len({user for user, _ in values}),
                   "interactions": sum(value for _, value in values)}
                  for (d, model), values in sorted(model_groups.items())]
    if hide_daily:
        daily = [{key: value if key == "day" else None for key, value in row.items()} for row in daily]
    if hide_users:
        licensed_count, active_count, adoption = None, None, None
        coverage = roster_coverage = "Suppressed, including complementary population"

    quality = [
        "Provisioned/enabled/engaged populations require separate approved sources and are unavailable.",
        "No telemetry is not proof of non-use. Model user counts and daily user counts are non-additive.",
        "Billing tokens exclude some Copilot surfaces; CLI/app tokens are separate overlapping telemetry.",
        "Net usage amounts exclude subscriptions, taxes, and other invoice items.",
        "Scoped managers and named-user drilldowns require an authenticated backend; not supported by this site.",
        "Usage cost-center user counts remain unavailable without verified dated allocation evidence.",
    ]
    for source in sources:
        if source["missing_days"]:
            quality.append(f"{source['label']}: {len(source['missing_days'])} missing daily partitions in this window.")
    # Preserve the source's daily count independently, and surface reconciliation discrepancies.
    for row in flat["enterprise_usage"]:
        observed = {r["user_id"] for r in parts["usage"].get(row["day"], [])}
        if row["day"] in parts["usage"] and row["active_users"] != len(observed):
            quality.append(f"{row['day']}: enterprise and per-user activity counts differ; coverage needs review.")
    billing = []
    if publication.get("billing_aggregates_approved") is True:
        billing = flat["billing"]
        if publication.get("cost_center_breakdowns_approved") is True:
            complete = all(d in parts["billing"] and d in parts["billing_centers"] for d in period)
            if complete and reconcile(billing, flat["billing_centers"]):
                billing = flat["billing_centers"]
                quality.append("Cost-center amounts reconcile exactly to matching enterprise totals.")
            else:
                quality.append("Cost-center billing unavailable: incomplete partitions or reconciliation mismatch.")
        else:
            quality.append("Cost-center billing withheld pending independent finance/privacy approval.")
    else:
        quality.append("Billing aggregates withheld pending independent finance/privacy approval (no user cohorts supplied by API).")

    token_groups = defaultdict(list)
    alias_ids = defaultdict(set)
    for snapshot_day, partition in parts["seats"].items():
        for row in partition["seats"]:
            if row["login"] and row["user_id"]:
                alias_ids[(snapshot_day, row["login"])].add(row["user_id"])
    for row in flat["usage"]:
        if row["login"]:
            alias_ids[(row["day"], row["login"])].add(row["user_id"])
    for row in flat["tokens"]:
        # Never infer historical billed allocation from today's membership or name.
        center_id, center_name, attribution = row["cost_center_id"], row["cost_center_name"], row["attribution"]
        if center_id == "unresolved":
            matches = [c for c in parts["cost_centers"].get(row["day"], []) if c["name"] == center_name]
            if len(matches) == 1:
                center_id, attribution = matches[0]["id"], "source-billed"
            else:
                center_name = "Unknown / unresolved"
        if not publication.get("cost_center_breakdowns_approved"):
            center_id, center_name, attribution = "__enterprise__", "Enterprise total", "unavailable"
        # Login-only records must have an unambiguous stable-ID mapping.
        user = row["user_id"]
        alias = (row["day"], row["login"])
        if not user and len(alias_ids.get(alias, set())) == 1:
            user = next(iter(alias_ids[alias]))
        token_groups[(row["day"], row["model"], center_id, center_name, attribution)].append((user, row))
    hide_tokens = any(
        any(user is None for user, _ in values) or len({user for user, _ in values}) < threshold
        for values in token_groups.values()
    )
    tokens = []
    if not hide_tokens:
        for (d, model, center_id, center_name, attribution), values in sorted(token_groups.items()):
            tokens.append({
                "day": d, "model": model, "cost_center_id": center_id, "cost_center_name": center_name,
                "attribution": attribution, "source": "tokens",
                **{category: total(row[category] for _, row in values) for category in TOKENS},
            })
    suppressed = hide_users or hide_daily or hide_models or hide_tokens
    if hide_tokens:
        quality.append("All token breakdowns withheld: unknown identities or cohorts below the approved threshold.")
    if hide_models:
        quality.append("All model activity breakdowns withheld to avoid reconstructing small complementary cohorts.")
    centers = {}
    for row in billing + tokens:
        centers[row["cost_center_id"]] = {"id": row["cost_center_id"], "name": row["cost_center_name"], "state": "unknown"}
    for center_list in parts["cost_centers"].values():
        for center in center_list:
            if center["id"] in centers:
                centers[center["id"]] = {key: center[key] for key in ("id", "name", "state")}
    successes = [source["last_successful_collection"] for source in sources if source["last_successful_collection"]]
    return {
        "schema_version": 1, "demo": demo, "generated_at": utc_now(),
        "last_successful_collection": max(successes) if successes else None,
        "period": {"start": start, "end": end, "timezone": "UTC"},
        "privacy": {"minimum_cohort": threshold, "breakdowns_suppressed": suppressed,
                    "suppressed_families": [name for name, hidden in (
                        ("overview", hide_users), ("daily", hide_daily),
                        ("models", hide_models), ("tokens", hide_tokens)) if hidden],
                    "policy_fingerprint": policy_fingerprint(publication),
                    "notice": "Only the common approved audience may receive this entire dataset. Small cohorts suppress whole metric families; privacy approval remains mandatory."},
        "sources": sources,
        "overview": {
            "licensed_users": metric(licensed_count, "distinct users", "seats", roster_coverage, start, end),
            "observed_active_users": metric(active_count, "distinct users", "usage", coverage, start, end),
            "adoption_rate": metric(adoption, "percent", "usage", "Date-matched licensed activity / period licensed users. " + roster_coverage, start, end),
            "interactions": metric(
                int(total(row["interactions"] for row in flat["usage"])) if flat["usage"] and usage_complete and not hide_users and total(row["interactions"] for row in flat["usage"]) is not None else (0 if usage_complete and not flat["usage"] and not hide_users else None),
                "interactions", "usage", coverage, start, end),
        },
        "daily": daily, "models": models, "billing": billing, "tokens": tokens,
        "cost_centers": list(centers.values()), "quality": quality,
    }
