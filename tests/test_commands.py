import argparse
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from copilot_reporting.__main__ import configuration, run
from copilot_reporting.aggregate import build_report
from copilot_reporting.demo import APPROVAL, END, START, snapshots
from copilot_reporting.publish import publish
from copilot_reporting.store import Store


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config_path = self.root / "config.json"
        self.config = {
            "enterprise": "synthetic", "enterprise_scope_verified": True,
            "publication": APPROVAL,
        }
        self.config_path.write_text(json.dumps(self.config))

    def tearDown(self):
        self.temporary.cleanup()

    def test_configuration_rejects_invalid_retention(self):
        for value in (True, 0, -1, 396, "30"):
            self.config["raw_retention_days"] = value
            self.config_path.write_text(json.dumps(self.config))
            with self.assertRaises(ValueError):
                configuration(self.config_path)

    def test_publication_requires_verified_enterprise_scope(self):
        self.config["enterprise_scope_verified"] = False
        self.config_path.write_text(json.dumps(self.config))
        args = argparse.Namespace(command="collect", config=self.config_path)
        with self.assertRaises(ValueError):
            run(args)

    def test_restore_requires_identical_approved_privacy_policy(self):
        directory = self.root / "store"
        store = Store(directory, "synthetic")
        for snapshot in snapshots():
            store.ingest(snapshot)
        store.save_aggregate(build_report(store, START, END, APPROVAL))
        store.close()
        args = argparse.Namespace(
            command="publish", config=self.config_path, store=directory,
            start=START, end=END, archived=True, output=self.root / "site",
        )
        with patch("copilot_reporting.__main__.publish") as mocked:
            self.assertEqual(run(args), 0)
            self.assertEqual(mocked.call_args.args[0]["overview"]["licensed_users"]["value"], 12)
        settings = copy.deepcopy(self.config)
        settings["publication"]["minimum_cohort"] = 10
        self.config_path.write_text(json.dumps(settings))
        with self.assertRaises(ValueError):
            run(args)

    def test_publication_rejects_unexpected_files_and_symlinks(self):
        site = self.root / "site"
        site.mkdir()
        (site / "private.sqlite").write_text("private")
        with self.assertRaises(ValueError):
            publish({}, site)
        (site / "private.sqlite").unlink()
        (site / "data").symlink_to(self.root)
        with self.assertRaises(ValueError):
            publish({}, site)

    def test_only_allowlisted_outputs_written(self):
        site = self.root / "site"
        with patch("copilot_reporting.publish.ASSETS", ()):
            publish({"schema_version": 1}, site)
            publish({"schema_version": 2}, site)
        self.assertEqual(json.loads((site / "data" / "report.json").read_text()), {"schema_version": 2})
        self.assertEqual(sorted(str(p.relative_to(site)) for p in site.rglob("*")), ["data", "data/report.json"])
        self.assertEqual(site.stat().st_mode & 0o777, 0o700)
        self.assertEqual((site / "data" / "report.json").stat().st_mode & 0o777, 0o600)

    def test_private_history_cannot_be_inside_another_git_tree(self):
        (self.root / ".git").mkdir()
        with self.assertRaises(ValueError):
            Store(self.root / "private", "synthetic")


if __name__ == "__main__":
    unittest.main()
