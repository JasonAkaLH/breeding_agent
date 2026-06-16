from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from src.state.cutover import FreshCutoverInput, build_postgres_fresh_cutover_plan, validate_cutover_report_is_redacted
from src.state.sqlite_cleanup import build_sqlite_cleanup_plan


class PostgreSQLFreshCutoverLegacyMigrationGuardTest(unittest.TestCase):
    def test_fresh_cutover_plan_is_redacted_and_has_no_sqlite_import_objects(self) -> None:
        plan = build_postgres_fresh_cutover_plan(
            FreshCutoverInput(
                postgres_dsn="postgresql://user:pass@example/db",
                schema_ready=True,
                runtime_smoke_ready=True,
                queue_backlog=0,
                dead_letter_count=0,
                sqlite_history_abandoned=True,
            )
        )
        public = plan.public_dict()
        self.assertTrue(plan.ready)
        self.assertNotIn("objects", public)
        self.assertNotIn("row_counts", public)
        self.assertNotIn("checksums", public)
        self.assertNotIn("postgresql://", repr(public))
        self.assertTrue(validate_cutover_report_is_redacted(public))

    def test_local_sqlite_cleanup_requires_operator_confirmation(self) -> None:
        with TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "state.db"
            sqlite_path.write_bytes(b"sqlite-fixture")
            plan = build_sqlite_cleanup_plan(runtime_dir=tmp, candidates=[sqlite_path])
            self.assertTrue(plan.dry_run)
            self.assertTrue(sqlite_path.exists())

    def test_legacy_migration_cli_rejects_raw_dsn_argument(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "scripts/postgresql_state_migration.py",
                "--postgres-dsn",
                "postgresql_fixture_dsn",
                "--json",
            ],
            check=False,
            text=True,
            capture_output=True,
        )
        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertIn("raw DSN CLI arguments are not allowed", payload["error"])

    def test_cutover_cli_does_not_accept_sqlite_import_path(self) -> None:
        with TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "state.db"
            sqlite_path.write_bytes(b"sqlite-fixture")
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/postgresql_state_cutover.py",
                    "--sqlite-path",
                    str(sqlite_path),
                    "--json",
                ],
                check=False,
                text=True,
                capture_output=True,
            )
        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertIn("SQLite import is not part of PostgreSQL fresh cutover", payload["error"])
