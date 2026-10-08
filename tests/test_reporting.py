import copy
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from copilot_reporting.aggregate import build_report, reconcile
from copilot_reporting.demo import APPROVAL, END, START, snapshots
from copilot_reporting.domain import number, total
from copilot_reporting.store import Store


class ReportingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(self.directory.name, "synthetic")
        self.data = list(snapshots())
        for snapshot in self.data:
            self.store.ingest(snapshot)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def report(self):
        return build_report(self.store, START, END, APPROVAL)

    def test_distinct_counts_across_grants_models_and_days(self):
        report = self.report()
        self.assertEqual(report["overview"]["licensed_users"]["value"], 12)
        self.assertEqual(report["overview"]["observed_active_users"]["value"], 12)
        self.assertEqual(report["overview"]["adoption_rate"]["value"], 100)
        self.assertEqual(report["overview"]["interactions"]["value"], 108)
        self.assertEqual(len(report["models"]), 3)

    def test_server_activity_with_empty_details_is_active(self):
        snapshot = self.data[0]
        for row in snapshot["sources"]["usage"]["data"]:
            row["user_initiated_interaction_count"] = 0
            row["totals_by_model_feature"] = []
        self.store.ingest(snapshot)
        self.assertEqual(self.report()["daily"][0]["active_users"], 12)

    def test_current_roster_never_backfills_missing_history(self):
        self.store.connection.execute("DELETE FROM partitions WHERE source='seats' AND day=?", (START,))
        report = self.report()
        self.assertIsNone(report["overview"]["licensed_users"]["value"])
        self.assertIsNone(report["overview"]["adoption_rate"]["value"])
        self.assertEqual(report["overview"]["observed_active_users"]["value"], 12)

    def test_replay_correction_replaces_and_failure_retains(self):
        for snapshot in self.data:
            self.store.ingest(snapshot)
        self.assertEqual(self.report()["tokens"][0]["input"], "600")
        change = copy.deepcopy(self.data[0])
        for row in change["sources"]["tokens"]["data"]:
            row["input"] = "200"
        self.store.ingest(change)
        self.assertEqual(self.report()["tokens"][0]["input"], "1200")
        change["sources"]["tokens"] = {"status": "error"}
        self.store.ingest(change)
        report = self.report()
        self.assertEqual(report["tokens"][0]["input"], "1200")
        self.assertEqual(next(s for s in report["sources"] if s["id"] == "tokens")["status"], "error")

    def test_duplicate_or_incomplete_source_never_replaces_good_data(self):
        change = copy.deepcopy(self.data[0])
        change["sources"]["usage"]["data"].append(change["sources"]["usage"]["data"][0])
        self.store.ingest(change)
        self.assertEqual(self.report()["overview"]["observed_active_users"]["value"], 12)
        self.assertEqual(self.store.statuses()["usage"]["status"], "error")
        change["sources"]["usage"] = {"status": "ok", "data": [], "missing_days": [START]}
        self.store.ingest(change)
        self.assertEqual(len(self.store.partitions("usage", START, START)[START]), 12)

    def test_privacy_suppresses_entire_token_family(self):
        change = copy.deepcopy(self.data[0])
        change["sources"]["tokens"]["data"][0]["model"] = "new-small-cohort"
        self.store.ingest(change)
        report = self.report()
        self.assertEqual(report["tokens"], [])
        self.assertTrue(report["privacy"]["breakdowns_suppressed"])

    def test_small_complementary_population_suppressed(self):
        change = copy.deepcopy(self.data[0])
        change["sources"]["usage"]["data"].pop()
        self.store.ingest(change)
        report = self.report()
        self.assertTrue(all(row["active_users"] is None for row in report["daily"]))
        self.assertEqual(report["models"], [])

    def test_absent_tokens_are_unavailable_not_zero(self):
        self.assertIsNone(self.report()["tokens"][0]["cache_write"])
        self.store.connection.execute("DELETE FROM partitions WHERE source='tokens'")
        self.assertEqual(self.report()["tokens"], [])

    def test_billing_partition_reconciliation_no_double_counting(self):
        report = self.report()
        self.assertEqual(total(row["net_amount"] for row in report["billing"]), "0.60")
        change = copy.deepcopy(self.data[0])
        change["sources"]["billing_centers"]["data"][0]["netAmount"] = "0.11"
        self.store.ingest(change)
        report = self.report()
        self.assertEqual(len(report["billing"]), 3)
        self.assertTrue(all(row["cost_center_id"] == "__enterprise__" for row in report["billing"]))

    def test_exact_decimal_and_invalid_numbers(self):
        self.assertEqual(total(["0.1", "0.2"]), "0.3")
        for value in ("NaN", "Infinity", "-1", True, "1e999", "0.00000000000001"):
            with self.assertRaises(ValueError):
                number(value)
        self.assertIsNone(total(["1", None]))

    def test_publication_requires_approval_and_common_audience(self):
        for settings in ({}, {**APPROVAL, "audience": "scoped"}, {**APPROVAL, "minimum_cohort": 1}):
            with self.assertRaises(ValueError):
                build_report(self.store, START, END, settings)

    def test_no_identity_or_unapproved_fields_in_published_data(self):
        change = copy.deepcopy(self.data[0])
        change["sources"]["usage"]["data"][0]["prompt"] = "SHOULD-NOT-PERSIST"
        change["sources"]["cost_centers"]["data"][0]["resources"] = [
            {"type": "User", "name": "PRIVATE-RESOURCE-NAME"}]
        self.store.ingest(change)
        encoded = json.dumps(self.report())
        for sensitive in ("synthetic-user", "user_id", "login", "SHOULD-NOT-PERSIST", "assignee", "PRIVATE-RESOURCE-NAME"):
            self.assertNotIn(sensitive, encoded)
        saved = json.dumps(self.store.partitions("usage", START, END))
        self.assertNotIn("SHOULD-NOT-PERSIST", saved)

    def test_retention_and_store_tenant_boundary(self):
        self.store.save_aggregate(self.report())
        self.store.expire(1, 395, date.fromisoformat(END))
        self.assertEqual(list(self.store.partitions("usage", START, END)), [END])
        self.assertEqual(self.store.archived_aggregate(START, END)["overview"]["licensed_users"]["value"], 12)
        with self.assertRaises(ValueError):
            Store(self.directory.name, "other-enterprise")

    def test_null_assignee_makes_roster_incomplete(self):
        change = copy.deepcopy(self.data[0])
        change["sources"]["seats"]["data"]["seats"][0]["assignee"] = None
        self.store.ingest(change)
        self.assertIsNone(self.report()["overview"]["adoption_rate"]["value"])

    def test_model_names_remain_dynamic(self):
        change = copy.deepcopy(self.data[0])
        for row in change["sources"]["usage"]["data"]:
            row["totals_by_model_feature"][0]["model"] = "future-automatic-model"
        self.store.ingest(change)
        self.assertEqual(self.report()["models"][0]["model"], "future-automatic-model")

    def test_ambiguous_name_mapping_remains_unresolved(self):
        change = copy.deepcopy(self.data[0])
        for row in change["sources"]["tokens"]["data"]:
            row.pop("cost_center_id")
            row["cost_center_name"] = "Same name"
        change["sources"]["cost_centers"]["data"] = [
            {"id": "one", "name": "Same name"}, {"id": "two", "name": "Same name", "state": "deleted"}]
        self.store.ingest(change)
        rows = [row for row in self.report()["tokens"] if row["day"] == START]
        self.assertEqual(rows[0]["cost_center_id"], "unresolved")
        self.assertEqual(rows[0]["attribution"], "unknown")

    def test_login_mapping_requires_matching_day(self):
        change = copy.deepcopy(self.data[0])
        for row in change["sources"]["tokens"]["data"]:
            row["username"] = f"later-login-{row.pop('user_id')}"
        self.store.ingest(change)
        later = copy.deepcopy(self.data[-1])
        for row in later["sources"]["usage"]["data"]:
            row["user_login"] = f"later-login-{row['user_id']}"
        self.store.ingest(later)
        self.assertEqual(self.report()["tokens"], [])


if __name__ == "__main__":
    unittest.main()
