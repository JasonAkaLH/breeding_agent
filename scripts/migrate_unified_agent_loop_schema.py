#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.storage.agent_schema_migration import (  # noqa: E402
    AgentSchemaMigrationError,
    apply_all,
    backup_all,
    build_report,
    load_state_descriptor,
    migration_lock,
    remember_report_path,
    restore_all,
    restore_check,
    verify_tested_revision,
    write_report,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Closed backup/apply/restore operator for unified Agent schema migration."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    report = subparsers.add_parser("report")
    report.add_argument("--state-root", required=True)
    report.add_argument("--output", required=True)

    backup = subparsers.add_parser("backup")
    backup.add_argument("--state-root", required=True)
    backup.add_argument("--report", required=True)
    backup.add_argument("--expected-report-sha", required=True)
    backup.add_argument("--backup-root", required=True)

    restore = subparsers.add_parser("restore-check")
    restore.add_argument("--state-root", required=True)
    restore.add_argument("--backup-manifest", required=True)
    restore.add_argument("--expected-backup-set-sha", required=True)
    restore.add_argument("--restore-root", required=True)

    restore_all_parser = subparsers.add_parser("restore-all")
    restore_all_parser.add_argument("--state-root", required=True)
    restore_all_parser.add_argument("--backup-manifest", required=True)
    restore_all_parser.add_argument("--expected-backup-set-sha", required=True)

    apply_parser = subparsers.add_parser("apply")
    apply_parser.add_argument("--state-root", required=True)
    apply_parser.add_argument("--report", required=True)
    apply_parser.add_argument("--expected-report-sha", required=True)
    apply_parser.add_argument("--backup-manifest", required=True)
    apply_parser.add_argument("--expected-backup-set-sha", required=True)
    apply_parser.add_argument("--restore-receipt", required=True)
    return parser


def run(args: argparse.Namespace) -> dict[str, object]:
    descriptor = load_state_descriptor(args.state_root)
    verify_tested_revision(descriptor, ROOT)
    with migration_lock(descriptor.state_root):
        if args.command == "report":
            output = Path(args.output).resolve()
            if output.parent != descriptor.state_root:
                raise AgentSchemaMigrationError("agent_schema_report_path_not_private")
            report = build_report(descriptor)
            write_report(output, report)
            remember_report_path(descriptor, output, report)
            return {
                "result": "reported",
                "report_sha256": report["report_sha256"],
                "blockers": report["blockers"],
            }
        if args.command == "backup":
            manifest = backup_all(
                descriptor,
                report_path=args.report,
                expected_report_sha=args.expected_report_sha,
                backup_root=args.backup_root,
            )
            return {
                "result": "backed_up",
                "backup_set_id": manifest["backup_set_id"],
                "backup_set_sha256": manifest["backup_set_sha256"],
            }
        if args.command == "restore-check":
            receipt = restore_check(
                descriptor,
                manifest_path=args.backup_manifest,
                expected_backup_set_sha=args.expected_backup_set_sha,
                restore_root=args.restore_root,
            )
            return {
                "result": "restore_verified",
                "backup_set_sha256": receipt["backup_set_sha256"],
            }
        if args.command == "restore-all":
            receipt = restore_all(
                descriptor,
                manifest_path=args.backup_manifest,
                expected_backup_set_sha=args.expected_backup_set_sha,
            )
            return {
                "result": "restored",
                "backup_set_sha256": receipt["backup_set_sha256"],
            }
        if args.command == "apply":
            receipt = apply_all(
                descriptor,
                report_path=args.report,
                expected_report_sha=args.expected_report_sha,
                manifest_path=args.backup_manifest,
                expected_backup_set_sha=args.expected_backup_set_sha,
                restore_receipt_path=args.restore_receipt,
            )
            return {
                "result": "completed",
                "backup_set_sha256": receipt["backup_set_sha256"],
                "receipt_sha256": receipt["receipt_sha256"],
            }
        raise AgentSchemaMigrationError("agent_schema_command_invalid")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload = run(args)
    except (AgentSchemaMigrationError, OSError, ValueError) as exc:
        reason = str(exc)
        if not reason.startswith("agent_schema_"):
            reason = "agent_schema_operator_failed"
        print(
            json.dumps(
                {"result": "rejected", "reason_code": reason},
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
