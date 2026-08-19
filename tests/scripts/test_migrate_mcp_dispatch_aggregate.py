from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from scripts.migrate_mcp_dispatch_aggregate import main, run_apply, run_report
from src.storage.mcp_dispatch_aggregate_migration import (
    MCPDispatchAggregateAuthorityConflictError,
    MCPDispatchAggregateMigrationError,
    apply_sqlite_dispatch_aggregate,
    build_postgres_aggregate_cutover_plan,
    create_or_adopt_sqlite_aggregate_backup,
    inspect_sqlite_dispatch_aggregate,
    validate_migration_transition,
)
from src.state.postgres.runtime_schema import (
    build_postgres_fresh_cutover_schema_manifest,
)
from src.state.postgres.schema_reconciler import SchemaInspection
from src.storage.sqlite import bootstrap_sqlite_database, create_sqlite_engine


class MCPDispatchAggregateMigrationScriptTest(unittest.TestCase):
    def _create_empty_legacy_database(self, path: Path) -> None:
        connection = sqlite3.connect(path)
        try:
            connection.executescript(
                "CREATE TABLE mcp_dispatch_resume_outbox ("
                "outbox_id TEXT PRIMARY KEY, intent_id TEXT, status TEXT);"
                "CREATE TABLE mcp_call_record ("
                "call_ref TEXT PRIMARY KEY, status TEXT);"
            )
            connection.commit()
        finally:
            connection.close()

    def test_fresh_sqlite_report_is_closed_redacted_and_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "runtime.sqlite3"
            engine = create_sqlite_engine(path)
            try:
                bootstrap_sqlite_database(engine)
            finally:
                engine.dispose()

            first = inspect_sqlite_dispatch_aggregate(path).as_payload()
            second = run_report(
                Namespace(report=True, database_path=str(path), dsn_env=None)
            )

            self.assertEqual(first, second)
            self.assertEqual(first["backend"], "sqlite")
            self.assertFalse(first["migration_required"])
            self.assertFalse(first["apply_eligible"])
            self.assertEqual(first["blocker_reason_codes"], [])
            self.assertNotIn(str(path), json.dumps(first, sort_keys=True))

    def test_legacy_business_rows_are_counted_without_exposing_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "legacy.sqlite3"
            connection = sqlite3.connect(path)
            try:
                connection.executescript(
                    "CREATE TABLE mcp_dispatch_resume_outbox ("
                    "outbox_id TEXT PRIMARY KEY, intent_id TEXT, status TEXT);"
                    "INSERT INTO mcp_dispatch_resume_outbox VALUES "
                    "('private-outbox-id','private-intent-id','claimed');"
                    "CREATE TABLE mcp_call_record ("
                    "call_ref TEXT PRIMARY KEY, status TEXT);"
                    "INSERT INTO mcp_call_record VALUES "
                    "('private-call-id','running');"
                )
                connection.commit()
            finally:
                connection.close()

            report = inspect_sqlite_dispatch_aggregate(path).as_payload()
            encoded = json.dumps(report, sort_keys=True)

            self.assertTrue(report["migration_required"])
            self.assertFalse(report["apply_eligible"])
            self.assertEqual(report["row_counts"]["mcp_call_record"], 1)
            self.assertEqual(
                report["status_counts"]["mcp_dispatch_resume_outbox"]["claimed"],
                1,
            )
            self.assertEqual(
                report["status_counts"]["mcp_call_record"]["other"], 1
            )
            self.assertNotIn("private-outbox-id", encoded)
            self.assertNotIn("private-intent-id", encoded)
            self.assertNotIn("private-call-id", encoded)
            self.assertNotIn("running", encoded)

    def test_sqlite_backup_is_no_clobber_and_exactly_adoptable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "runtime.sqlite3"
            engine = create_sqlite_engine(path)
            try:
                bootstrap_sqlite_database(engine)
            finally:
                engine.dispose()
            report_sha = inspect_sqlite_dispatch_aggregate(path).as_payload()[
                "report_sha256"
            ]

            created = create_or_adopt_sqlite_aggregate_backup(
                path,
                report_sha256=report_sha,
                migration_status="planned",
            )
            self.assertIsNotNone(created)
            backup_path = path.with_name(created.basename)
            self.assertEqual(os.stat(backup_path).st_mode & 0o777, 0o600)
            self.assertTrue(
                created.basename.startswith(
                    "runtime.sqlite3.pre-mcp-aggregate-v1."
                )
            )

            adopted = create_or_adopt_sqlite_aggregate_backup(
                path,
                report_sha256=report_sha,
                migration_status="backed_up",
                expected_backup_sha256=created.sha256,
            )
            self.assertEqual(adopted, created)
            with self.assertRaises(MCPDispatchAggregateMigrationError):
                create_or_adopt_sqlite_aggregate_backup(
                    path,
                    report_sha256=report_sha,
                    migration_status="planned",
                )

    def test_backup_adoption_rejects_mode_link_and_digest_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "runtime.sqlite3"
            engine = create_sqlite_engine(path)
            try:
                bootstrap_sqlite_database(engine)
            finally:
                engine.dispose()
            report_sha = inspect_sqlite_dispatch_aggregate(path).as_payload()[
                "report_sha256"
            ]
            created = create_or_adopt_sqlite_aggregate_backup(
                path,
                report_sha256=report_sha,
                migration_status="planned",
            )
            backup_path = path.with_name(created.basename)
            os.chmod(backup_path, 0o640)
            with self.assertRaises(MCPDispatchAggregateMigrationError):
                create_or_adopt_sqlite_aggregate_backup(
                    path,
                    report_sha256=report_sha,
                    migration_status="backed_up",
                    expected_backup_sha256=created.sha256,
                )

    def test_in_memory_backup_is_explicitly_skipped(self) -> None:
        self.assertIsNone(
            create_or_adopt_sqlite_aggregate_backup(
                ":memory:",
                report_sha256="sha256:" + "a" * 64,
                migration_status="planned",
            )
        )

    def test_migration_state_machine_rejects_skips_and_terminal_reentry(self) -> None:
        validate_migration_transition("planned", "backed_up")
        validate_migration_transition("backed_up", "applying")
        validate_migration_transition("applying", "applied")
        with self.assertRaises(MCPDispatchAggregateMigrationError):
            validate_migration_transition("planned", "applied")
        with self.assertRaises(MCPDispatchAggregateMigrationError):
            validate_migration_transition("applied", "applying")

    def test_report_mode_rejects_ambiguous_backend_options(self) -> None:
        with self.assertRaises(MCPDispatchAggregateMigrationError):
            run_report(
                Namespace(
                    report=True,
                    database_path="runtime.sqlite3",
                    dsn_env="CP7_POSTGRES_VALIDATION_DSN",
                )
            )

    def test_apply_empty_legacy_sqlite_is_atomic_recorded_and_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "legacy.sqlite3"
            self._create_empty_legacy_database(path)
            before = inspect_sqlite_dispatch_aggregate(path).as_payload()
            report_sha = str(before["report_sha256"])

            applied = run_apply(
                Namespace(
                    apply=True,
                    database_path=str(path),
                    dsn_env=None,
                    expected_report_sha=report_sha.removeprefix("sha256:"),
                )
            )

            self.assertEqual(applied["result"], "applied")
            after = inspect_sqlite_dispatch_aggregate(path).as_payload()
            self.assertFalse(after["migration_required"])
            connection = sqlite3.connect(path)
            try:
                row = connection.execute(
                    "SELECT report_sha256, status, revision, backup_basename, "
                    "backup_sha256 FROM mcp_dispatch_aggregate_migration"
                ).fetchone()
            finally:
                connection.close()
            self.assertEqual(row[0], report_sha)
            self.assertEqual(row[1], "applied")
            self.assertEqual(row[2], 3)
            self.assertTrue(row[3])
            self.assertTrue(str(row[4]).startswith("sha256:"))
            self.assertTrue(path.with_name(row[3]).is_file())

            retried = apply_sqlite_dispatch_aggregate(
                path,
                expected_report_sha256=report_sha,
            ).as_payload()
            self.assertEqual(retried["result"], "already_applied")

    def test_apply_rejects_report_drift_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "legacy.sqlite3"
            self._create_empty_legacy_database(path)
            with self.assertRaisesRegex(
                MCPDispatchAggregateMigrationError,
                "report_changed",
            ):
                apply_sqlite_dispatch_aggregate(
                    path,
                    expected_report_sha256="0" * 64,
                )
            connection = sqlite3.connect(path)
            try:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
            finally:
                connection.close()
            self.assertNotIn("mcp_dispatch_aggregate_migration", tables)

    def test_sqlite_apply_rolls_back_table_swap_and_retries_from_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "legacy.sqlite3"
            self._create_empty_legacy_database(path)
            report_sha = str(
                inspect_sqlite_dispatch_aggregate(path).as_payload()[
                    "report_sha256"
                ]
            )
            from src.storage import mcp_dispatch_aggregate_migration as migration

            original_replace = migration._replace_empty_sqlite_table
            calls = 0

            def fail_after_first_swap(connection, table_name):
                nonlocal calls
                original_replace(connection, table_name)
                calls += 1
                if calls == 1:
                    raise RuntimeError("injected_swap_failure")

            with patch.object(
                migration,
                "_replace_empty_sqlite_table",
                side_effect=fail_after_first_swap,
            ):
                with self.assertRaisesRegex(RuntimeError, "injected_swap_failure"):
                    apply_sqlite_dispatch_aggregate(
                        path,
                        expected_report_sha256=report_sha,
                    )

            rolled_back = inspect_sqlite_dispatch_aggregate(path).as_payload()
            self.assertTrue(rolled_back["migration_required"])
            connection = sqlite3.connect(path)
            try:
                status = connection.execute(
                    "SELECT status FROM mcp_dispatch_aggregate_migration"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(status, "backed_up")

            retried = apply_sqlite_dispatch_aggregate(
                path,
                expected_report_sha256=report_sha,
            )
            self.assertEqual(retried.result, "applied")

    def test_apply_business_rows_is_authority_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "legacy.sqlite3"
            self._create_empty_legacy_database(path)
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "INSERT INTO mcp_call_record VALUES ('private-call', 'active')"
                )
                connection.commit()
            finally:
                connection.close()
            report = inspect_sqlite_dispatch_aggregate(path).as_payload()
            with self.assertRaises(MCPDispatchAggregateAuthorityConflictError):
                apply_sqlite_dispatch_aggregate(
                    path,
                    expected_report_sha256=str(report["report_sha256"]),
                )

    def test_cli_exit_codes_are_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "legacy.sqlite3"
            self._create_empty_legacy_database(path)
            self.assertEqual(
                main(
                    [
                        "--apply",
                        "--database-path",
                        str(path),
                        "--expected-report-sha",
                        "0" * 64,
                    ]
                ),
                2,
            )
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "INSERT INTO mcp_call_record VALUES ('private-call', 'active')"
                )
                connection.commit()
            finally:
                connection.close()
            blocked = inspect_sqlite_dispatch_aggregate(path).as_payload()
            self.assertEqual(
                main(
                    [
                        "--apply",
                        "--database-path",
                        str(path),
                        "--expected-report-sha",
                        str(blocked["report_sha256"]).removeprefix("sha256:"),
                    ]
                ),
                3,
            )

    def test_postgres_cutover_plan_uses_bounded_lock_and_not_valid_replacement(
        self,
    ) -> None:
        manifest = build_postgres_fresh_cutover_schema_manifest()
        inspection = SchemaInspection.from_manifest(manifest)
        tables = {
            name: dict(columns) for name, columns in inspection.tables.items()
        }
        tables["mcp_dispatch_resume_outbox"].pop("resume_reason")
        tables["mcp_call_record"].pop("pending_action_id")
        checks = {
            table: dict(constraints)
            for table, constraints in inspection.check_constraints.items()
        }
        status_name = next(
            name
            for name in checks["mcp_dispatch_resume_outbox"]
            if name.endswith("mcp_dispatch_resume_status")
        )
        checks["mcp_dispatch_resume_outbox"][status_name] = (
            "status IN ('pending', 'claimed', 'completed', 'aborted')"
        )
        inspection = SchemaInspection(
            tables=tables,
            enum_types=inspection.enum_types,
            check_constraints=checks,
            triggers=inspection.triggers,
        )

        plan = build_postgres_aggregate_cutover_plan(inspection)
        sql = "\n".join(plan.statements)

        self.assertIn("lock_timeout = '3s'", sql)
        self.assertIn("statement_timeout = '30s'", sql)
        self.assertIn("pg_advisory_xact_lock", sql)
        self.assertIn("ADD COLUMN resume_reason", sql)
        self.assertIn("ADD COLUMN pending_action_id", sql)
        self.assertIn("NOT VALID", sql)
        self.assertLess(
            sql.index("VALIDATE CONSTRAINT"),
            sql.index(f"DROP CONSTRAINT IF EXISTS {status_name}"),
        )
        self.assertTrue(plan.plan_sha256.startswith("sha256:"))
