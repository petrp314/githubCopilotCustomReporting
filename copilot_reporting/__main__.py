"""Operator commands. Credentials and signed links are never printed."""

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .aggregate import build_report, policy_fingerprint, retain_last_good
from .domain import days, utc_now
from .publish import publish
from .store import Store


def configuration(path):
    with open(path, encoding="utf-8") as handle:
        config = json.load(handle)
    if not isinstance(config, dict):
        raise ValueError("Configuration must be an object")
    for key, default, limit in (("raw_retention_days", 35, 395), ("aggregate_retention_days", 395, 395),
                                ("replay_days", 7, 31), ("source_lag_days", 3, 31)):
        value = config.setdefault(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= limit:
            raise ValueError("Invalid retention or replay configuration")
    if config["replay_days"] > config["raw_retention_days"]:
        raise ValueError("Replay cannot exceed permitted raw retention")
    if not config.get("enterprise") or config["enterprise"].startswith("replace-"):
        raise ValueError("An approved enterprise is required")
    return config


def parser():
    command = argparse.ArgumentParser(description="Protected Copilot collection and aggregate-only publication")
    commands = command.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo", help="Build explicitly synthetic data; no network or credentials")
    demo.add_argument("--output", required=True)
    for name in ("collect", "import-tokens", "publish", "expire"):
        item = commands.add_parser(name)
        item.add_argument("--config", required=True)
        item.add_argument("--store", required=True)
        if name in ("collect", "import-tokens", "publish"):
            item.add_argument("--start")
            item.add_argument("--end")
        if name == "publish":
            item.add_argument("--output", required=True)
            item.add_argument("--archived", action="store_true", help="Restore an exact previously approved window")
        if name == "import-tokens":
            item.add_argument("--file", required=True)
    return command


def run(args):
    if args.command == "demo":
        from .demo import APPROVAL, END, START, snapshots
        with tempfile.TemporaryDirectory(prefix="copilot-demo-") as directory:
            store = Store(directory, "synthetic")
            try:
                for snapshot in snapshots():
                    store.ingest(snapshot)
                publish(build_report(store, START, END, APPROVAL, demo=True), args.output)
            finally:
                store.close()
        print("Synthetic aggregate dashboard built.")
        return 0
    config = configuration(args.config)
    if args.command != "expire" and config.get("enterprise_scope_verified") is not True:
        raise ValueError("Verify the credential's full intended enterprise visibility before using live data")
    store = Store(args.store, config["enterprise"], config.get("api_origin", "https://api.github.com"))
    try:
        if args.command == "expire":
            store.expire(config["raw_retention_days"], config["aggregate_retention_days"])
            # Export checkpoints may contain token rows; expire them under the same policy.
            cutoff = datetime.now(timezone.utc).timestamp() - config["raw_retention_days"] * 86400
            for path in (Path(args.store) / "exports").glob("*.json"):
                if path.is_file() and not path.is_symlink() and path.stat().st_mtime < cutoff:
                    path.unlink()
            print("Local retention applied; external backups require their own expiry policy.")
            return 0
        today = datetime.now(timezone.utc).date()
        end = args.end or (today - timedelta(days=config["source_lag_days"])).isoformat()
        start = args.start or (datetime.fromisoformat(end).date() - timedelta(days=config["replay_days"]-1)).isoformat()
        days(start, end)
        if args.command in ("collect", "import-tokens") and (today - datetime.fromisoformat(start).date()).days >= config["raw_retention_days"]:
            raise ValueError("Requested range exceeds approved raw retention")
        if args.command == "collect":
            from .collector import Collector
            snapshot = Collector(config, Path(args.store)).collect(start, end)
            snapshot["api_version"] = config.get("api_version", "2026-03-10")
            store.ingest(snapshot)
            statuses = store.statuses()
            failures = sum(item["status"] != "ok" for item in statuses.values())
            print(f"Collection recorded; {failures} sources unavailable or failed. Last good partitions retained.")
            return 2 if failures else 0
        if args.command == "import-tokens":
            from .token_import import read_tokens
            if not args.start or not args.end:
                raise ValueError("Imports require the complete declared replacement date range")
            try:
                data = read_tokens(Path(args.file), start, end, max_bytes=config.get("max_response_bytes", 20_000_000),
                                   max_rows=config.get("max_rows", 100_000))
            except (ValueError, OSError):
                store.ingest({"schema_version": 1, "enterprise": config["enterprise"], "collected_at": utc_now(),
                              "start_day": start, "end_day": end, "sources": {"tokens": {"status": "error"}}})
                raise ValueError("Token import rejected; audit status recorded") from None
            store.ingest({"schema_version": 1, "enterprise": config["enterprise"], "collected_at": utc_now(),
                          "start_day": start, "end_day": end, "sources": {"tokens": {"status": "ok", "data": data}}})
            print("Token import recorded as an authoritative period replacement; inspect source status before publication.")
            return 0 if store.statuses()["tokens"]["status"] == "ok" else 2
        approval = config.get("publication", {})
        if args.archived:
            if approval.get("approved") is not True or approval.get("audience") != "shared-aggregates":
                raise ValueError("Archive restoration requires approved common-audience publication")
            report = store.archived_aggregate(start, end)
            if report["privacy"].get("policy_fingerprint") != policy_fingerprint(approval):
                raise ValueError("Archive privacy policy differs; reapproval and regeneration are required")
        else:
            report = build_report(store, start, end, approval)
            if not args.start and not args.end:
                report = retain_last_good(report, store.latest_aggregate())
        destination = Path(args.output).resolve()
        private = Path(args.store).resolve()
        if destination == private or private in destination.parents or destination in private.parents:
            raise ValueError("Publication and protected storage must be separate")
        publish(report, destination)
        store.save_aggregate(report)
        print("Approved aggregate-only snapshot published.")
        return 0
    finally:
        store.close()


def main():
    os.umask(0o077)
    try:
        return run(parser().parse_args())
    except (ValueError, KeyError, TypeError, OSError):
        print("Operation failed validation or access checks; no sensitive details logged. Check configuration and source status.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
