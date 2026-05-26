from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from src.state.migration import build_sqlite_to_postgres_migration_plan, validate_migration_report_is_redacted


class SQLiteToPostgresMigrationTest(unittest.TestCase):
    def test_dry_run_plan_is_redacted_and_covers_required_objects(self) -> None:
        with TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "state.db"
            sqlite_path.write_bytes(b"sqlite-fixture")
            plan = build_sqlite_to_postgres_migration_plan(
                sqlite_path=sqlite_path,
                postgres_dsn="postgresql_fixture_dsn",
                dry_run=True,
            )
        self.assertTrue(plan.dry_run)
        self.assertFalse(plan.writes_enabled)
        for name in ("conversation", "message", "task", "event", "artifact", "interrupt", "mailbox", "auth_user_token"):
            self.assertIn(name, plan.objects)
        public = plan.public_dict()
        self.assertNotIn("user:pass", repr(public))
        self.assertNotIn("postgresql://", repr(public))
        self.assertTrue(validate_migration_report_is_redacted(public))

    def test_cutover_plan_requires_operator_confirmation(self) -> None:
        with TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "state.db"
            sqlite_path.write_bytes(b"sqlite-fixture")
            with self.assertRaisesRegex(ValueError, "operator confirmation"):
                build_sqlite_to_postgres_migration_plan(
                    sqlite_path=sqlite_path,
                    postgres_dsn="postgresql_fixture_dsn",
                    dry_run=False,
                    operator_confirmation=False,
                )
    def test_migration_cli_rejects_raw_dsn_argument(self) -> None:
        with TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "state.db"
            sqlite_path.write_bytes(b"sqlite-fixture")
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/postgresql_state_migration.py",
                    "--sqlite-path",
                    str(sqlite_path),
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
