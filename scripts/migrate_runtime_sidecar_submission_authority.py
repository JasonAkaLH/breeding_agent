#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.storage.runtime_sidecar_submission_migration import (  # noqa: E402
    SubmissionAuthorityMigrationError,
    _load_config,
    _load_json_secure,
    _write_report,
    apply_submission_authority_migration,
    build_submission_authority_report,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Closed offline operator for submission authority migration."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    report = commands.add_parser("report")
    report.add_argument("--config", required=True)
    report.add_argument("--output", required=True)

    apply = commands.add_parser("apply")
    apply.add_argument("--config", required=True)
    apply.add_argument("--report", required=True)
    apply.add_argument("--expected-report-sha256", required=True)
    apply.add_argument("--evidence-output", required=True)
    apply.add_argument("--receipt-output", required=True)
    apply.add_argument("--backup-output", required=True)
    return parser


def run(args: argparse.Namespace) -> dict[str, object]:
    config = _load_config(Path(args.config))
    if args.command == "report":
        report = build_submission_authority_report(config)
        _write_report(Path(args.output), report)
        return {
            "result": "reported",
            "report_sha256": report["report_sha256"],
            "blockers": report["blockers"],
        }
    if args.command == "apply":
        report = _load_json_secure(Path(args.report))
        receipt = apply_submission_authority_migration(
            config,
            report,
            args.expected_report_sha256,
            evidence_path=Path(args.evidence_output),
            receipt_path=Path(args.receipt_output),
            backup_path=Path(args.backup_output),
        )
        return {
            "result": "completed",
            "report_sha256": receipt["report_sha256"],
            "finalization_receipt_sha256": receipt["import_receipt"][
                "finalization_receipt_sha256"
            ],
        }
    raise SubmissionAuthorityMigrationError(
        "submission_authority_command_invalid"
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run(args)
    except (SubmissionAuthorityMigrationError, OSError, RuntimeError, ValueError) as exc:
        reason = str(exc)
        if not reason.startswith("submission_authority_"):
            reason = "submission_authority_operator_failed"
        print(
            json.dumps(
                {"result": "rejected", "reason_code": reason},
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 2
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
