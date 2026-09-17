from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
import contextlib
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts import migrate_runtime_sidecar_submission_authority as cli


class MigrateRuntimeSidecarSubmissionAuthorityCLITest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source.sqlite"
        self.sidecar = self.root / "sidecar.sqlite"
        self.importer = self.root / "importer"
        self.key = self.root / "key"
        self.task_evidence = self.root / "task-evidence.json"
        self.config = self.root / "config.json"
        self.commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip()
        self.tree = subprocess.check_output(
            ["git", "rev-parse", "HEAD^{tree}"], text=True
        ).strip()
        self._create_databases()
        self.importer.write_text("binary", encoding="utf-8")
        self.importer.chmod(0o700)
        self.key.write_bytes(b"x" * 32)
        self.key.chmod(0o600)
        self.task_evidence.write_text("{}", encoding="utf-8")
        self.task_evidence.chmod(0o600)
        self._write_config()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_report_writes_private_artifact_and_safe_stdout(self) -> None:
        output = self.root / "report.json"
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            exit_code = cli.main(
                [
                    "report",
                    "--config",
                    str(self.config),
                    "--output",
                    str(output),
                ]
            )

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["result"], "reported")
        self.assertNotIn(str(self.source), stdout.getvalue())
        self.assertNotIn(str(self.sidecar), stdout.getvalue())
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)

    def test_config_rejects_raw_postgres_dsn_and_unsafe_mode_without_leak(self) -> None:
        value = json.loads(self.config.read_text(encoding="utf-8"))
        value["postgres_dsn"] = "postgresql://secret@example.invalid/database"
        self.config.write_text(json.dumps(value), encoding="utf-8")
        self.config.chmod(0o600)
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = cli.main(
                [
                    "report",
                    "--config",
                    str(self.config),
                    "--output",
                    str(self.root / "report.json"),
                ]
            )
        self.assertEqual(exit_code, 2)
        self.assertNotIn("secret", stdout.getvalue())

        value.pop("postgres_dsn")
        self.config.write_text(json.dumps(value), encoding="utf-8")
        self.config.chmod(0o644)
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = cli.main(
                [
                    "report",
                    "--config",
                    str(self.config),
                    "--output",
                    str(self.root / "report.json"),
                ]
            )
        self.assertEqual(exit_code, 2)

    def test_apply_routes_only_approved_paths_to_core(self) -> None:
        report_path = self.root / "report.json"
        report_path.write_text("{}", encoding="utf-8")
        report_path.chmod(0o600)
        receipt = {
            "report_sha256": "a" * 64,
            "import_receipt": {"finalization_receipt_sha256": "b" * 64},
        }
        with (
            patch.object(cli, "_load_config", return_value="config") as load_config,
            patch.object(cli, "_load_json_secure", return_value={"report": True}),
            patch.object(
                cli,
                "apply_submission_authority_migration",
                return_value=receipt,
            ) as apply,
        ):
            result = cli.run(
                cli.build_parser().parse_args(
                    [
                        "apply",
                        "--config",
                        str(self.config),
                        "--report",
                        str(report_path),
                        "--expected-report-sha256",
                        "a" * 64,
                        "--evidence-output",
                        str(self.root / "evidence.json"),
                        "--receipt-output",
                        str(self.root / "receipt.json"),
                        "--backup-output",
                        str(self.root / "backup.sqlite"),
                    ]
                )
            )

        self.assertEqual(result["result"], "completed")
        load_config.assert_called_once_with(self.config)
        apply.assert_called_once()

    def test_config_hardlink_is_rejected(self) -> None:
        alias = self.root / "config-hardlink.json"
        os.link(self.config, alias)
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            exit_code = cli.main(
                [
                    "report",
                    "--config",
                    str(self.config),
                    "--output",
                    str(self.root / "hardlink-report.json"),
                ]
            )

        self.assertEqual(exit_code, 2)
        self.assertEqual(json.loads(stdout.getvalue())["result"], "rejected")

    def _write_config(self) -> None:
        self.config.write_text(
            json.dumps(
                {
                    "schema": "maf.submission_authority.migration_config.v1",
                    "source_backend": "sqlite",
                    "sqlite_path": self.source.name,
                    "postgres_dsn_env": None,
                    "sidecar_path": self.sidecar.name,
                    "importer_binary_path": self.importer.name,
                    "hmac_key_path": self.key.name,
                    "task_authority_evidence_path": self.task_evidence.name,
                    "key_id": "test-key",
                    "expected_tested_commit": self.commit,
                    "expected_tested_tree": self.tree,
                }
            ),
            encoding="utf-8",
        )
        self.config.chmod(0o600)

    def _create_databases(self) -> None:
        with contextlib.closing(sqlite3.connect(self.source)) as connection:
            connection.executescript(
                """
                CREATE TABLE conversation(
                    conversation_id TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    status TEXT NOT NULL,
                    current_task_id TEXT,
                    updated_at TEXT
                );
                CREATE TABLE message(
                    message_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    task_id TEXT,
                    created_at TEXT,
                    message_type TEXT NOT NULL
                );
                """
            )
        with contextlib.closing(sqlite3.connect(self.sidecar)) as connection:
            connection.execute(
                "CREATE TABLE submitted_tasks(task_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, root_message_id TEXT, status TEXT, routing_mode TEXT, requested_capability_id TEXT, summary TEXT, cancel_requested_at TEXT, created_at TEXT, updated_at TEXT, route_mode TEXT, real_path TEXT, shadow_path TEXT, config_version TEXT, reason_code TEXT, cohort_id TEXT, assignment_key_hash TEXT, assigned_at TEXT)"
            )
            connection.commit()
        self.source.chmod(0o600)
        self.sidecar.chmod(0o600)


if __name__ == "__main__":
    unittest.main()
