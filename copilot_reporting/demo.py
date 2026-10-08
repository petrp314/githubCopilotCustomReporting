"""Deterministic synthetic data; never a fallback for failed live collection."""

from .domain import days


START, END = "2026-10-01", "2026-10-03"
APPROVAL = {
    "approved": True, "audience": "shared-aggregates", "minimum_cohort": 5,
    "billing_aggregates_approved": True, "cost_center_breakdowns_approved": True,
}


def snapshots():
    for index, d in enumerate(days(START, END)):
        collected = d + "T23:59:00+00:00"
        users = [{"id": i, "login": f"synthetic-user-{i}"} for i in range(1, 13)]
        usage, tokens = [], []
        for user in users:
            usage.append({
                "day": d, "user_id": user["id"], "user_login": user["login"],
                "user_initiated_interaction_count": index + 2,
                "totals_by_model_feature": [{"model": "example-model", "user_initiated_interaction_count": index + 2}],
                "totals_by_cli": {"token_usage": {"prompt_tokens_sum": 30, "output_tokens_sum": 10}},
                "totals_by_copilot_app": {"token_usage": {"prompt_tokens_sum": 0, "output_tokens_sum": 0}},
            })
            tokens.append({
                "day": d, "model": "example-model", "user_id": user["id"],
                "cost_center_id": "engineering" if user["id"] <= 6 else "enterprise-only",
                "cost_center_name": "Engineering (synthetic)" if user["id"] <= 6 else "Enterprise Only",
                "input": "100", "output": "20", "cache_read": "15", "cache_write": None,
            })
        def billing(center, name, quantity, amount):
            return {
                "day": d, "model": "example-model", "product": "Copilot", "sku": "synthetic-ai-credit",
                "unitType": "AI credits", "currency": "USD", "pricePerUnit": "0.01",
                "grossQuantity": quantity, "discountQuantity": "0", "netQuantity": quantity,
                "grossAmount": amount, "discountAmount": "0", "netAmount": amount,
                "cost_center_id": center, "cost_center_name": name,
            }
        data = {
            "seats": {"total_seats": 12, "seats": [
                {"assignee": user, "assigning_organization": {"id": 1}, "created_at": d} for user in users
            ] + [{"assignee": users[0], "assigning_organization": {"id": 2}, "created_at": d}]},
            "cost_centers": [{"id": "engineering", "name": "Engineering (synthetic)", "state": "active"}],
            "usage": usage,
            "enterprise_usage": [{"day": d, "daily_active_users": 12, "user_initiated_interaction_count": 12 * (index + 2)}],
            "billing": [billing("__enterprise__", "Enterprise total", "20", "0.20")],
            "billing_centers": [
                billing("engineering", "Engineering (synthetic)", "10", "0.10"),
                billing("enterprise-only", "Enterprise Only", "10", "0.10"),
            ],
            "tokens": tokens,
        }
        yield {
            "schema_version": 1, "enterprise": "synthetic", "collected_at": collected,
            "start_day": d, "end_day": d,
            "sources": {source: {"status": "ok", "scope": "enterprise", "collected_at": collected, "data": value}
                        for source, value in data.items()},
        }
