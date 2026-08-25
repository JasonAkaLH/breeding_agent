from __future__ import annotations

import io
import json
import os
import sqlite3
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scripts import migrate_mcp_dispatch_aggregate as migration_script
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
    class _TrackingEnvironment:
        def __init__(self, values: dict[str, str], events: list[str]) -> None:
            self._values = values
            self._events = events

        def keys(self):
            self._events.append("env_snapshot")
            return self._values.keys()

        def __getitem__(self, key: str) -> str:
            return self._values[key]

        def get(self, key: str, default=None):
            self._events.append(f"dsn_read:{key}")
            return self._values.get(key, default)

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

    def _create_populated_supported_legacy_database(self, path: Path) -> None:
        connection = sqlite3.connect(path)
        try:
            connection.executescript(
                "CREATE TABLE mcp_call_record ("
                "call_ref TEXT PRIMARY KEY, branch_id TEXT NOT NULL, "
                "owner_user_id TEXT NOT NULL, task_id TEXT NOT NULL, "
                "node_id TEXT NOT NULL, server_id TEXT NOT NULL, "
                "tool_name TEXT NOT NULL, status TEXT NOT NULL, "
                "call_sequence INTEGER NOT NULL, arguments_sha256 TEXT NOT NULL, "
                "server_security_version INTEGER NOT NULL, "
                "server_config_version INTEGER, input_schema_sha256 TEXT NOT NULL, "
                "protocol_version TEXT, input_field_names TEXT, "
                "may_have_dispatched INTEGER NOT NULL, result_ref TEXT, "
                "output_size_bytes INTEGER, safe_error_code TEXT, "
                "created_at TEXT, updated_at TEXT, terminal_at TEXT);"
                "CREATE TABLE mcp_dispatch_resume_outbox ("
                "outbox_id TEXT PRIMARY KEY, intent_id TEXT NOT NULL, "
                "owner_user_id TEXT NOT NULL, task_id TEXT NOT NULL, "
                "node_id TEXT NOT NULL, server_id TEXT NOT NULL, "
                "resume_envelope_sha256 TEXT NOT NULL, payload_sha256 TEXT NOT NULL, "
                "status TEXT NOT NULL, claim_owner TEXT, claim_token TEXT, "
                "lease_expires_at TEXT, revision INTEGER NOT NULL, "
                "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
                "completed_at TEXT, result_receipt_id TEXT, completion_mode TEXT);"
                "INSERT INTO mcp_call_record VALUES ("
                "'private-call','private-branch','private-owner','private-task',"
                "'private-node','private-server','private-tool','active',1,"
                "'sha256:arguments',1,1,'sha256:schema','2025-06-18','[]',1,"
                "NULL,NULL,NULL,'2026-08-18T00:00:00+00:00',"
                "'2026-08-18T00:00:00+00:00',NULL);"
                "INSERT INTO mcp_dispatch_resume_outbox VALUES ("
                "'private-outbox','private-intent','private-owner','private-task',"
                "'private-node','private-server','sha256:envelope','sha256:payload',"
                "'claimed','private-claim-owner','private-claim-token',"
                "'2026-08-18T00:01:00+00:00',2,"
                "'2026-08-18T00:00:00+00:00','2026-08-18T00:00:00+00:00',"
                "NULL,NULL,NULL);"
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

    def test_postgres_report_and_apply_preserve_env_order_and_lifecycle(
        self,
    ) -> None:
        dsn_env = "CP7_POSTGRES_VALIDATION_DSN"
        raw_dsn = " postgresql+psycopg://operator "
        dsn = raw_dsn.strip()
        config = SimpleNamespace(
            backend=migration_script.StatePlatformBackend.POSTGRESQL,
            dsn=dsn,
        )
        expected_env = {
            dsn_env: raw_dsn,
            "MAF_STATE_STORE_BACKEND": "postgresql",
            "MAF_POSTGRES_STATE_DSN": dsn,
        }
        cases = (
            (
                "report",
                run_report,
                Namespace(
                    report=True,
                    expected_report_sha=None,
                    database_path=None,
                    dsn_env=dsn_env,
                ),
                "inspect_postgres_dispatch_aggregate",
                {"backend": "postgresql"},
                ["env_snapshot", f"dsn_read:{dsn_env}"],
            ),
            (
                "apply",
                run_apply,
                Namespace(
                    apply=True,
                    expected_report_sha="a" * 64,
                    database_path=None,
                    dsn_env=dsn_env,
                ),
                "apply_postgres_dispatch_aggregate",
                {"result": "applied"},
                [f"dsn_read:{dsn_env}", "env_snapshot"],
            ),
        )

        for mode, runner, args, operation_name, payload, expected_events in cases:
            with self.subTest(mode=mode):
                events: list[str] = []
                environment = self._TrackingEnvironment({dsn_env: raw_dsn}, events)
                engine = Mock()
                result = Mock()
                result.as_payload.return_value = payload
                with (
                    patch(
                        "src.storage.postgres.create_postgres_engine",
                        return_value=engine,
                    ) as create_engine,
                    patch.object(
                        migration_script,
                        "build_state_platform_runtime_config",
                        return_value=config,
                    ) as build_config,
                    patch.object(
                        migration_script,
                        operation_name,
                        return_value=result,
                    ) as operation,
                    patch.object(migration_script.os, "environ", environment),
                ):
                    self.assertEqual(runner(args), payload)

                self.assertEqual(events, expected_events)
                build_config.assert_called_once_with(
                    env=expected_env,
                    require_driver=True,
                )
                create_engine.assert_called_once_with(dsn)
                if mode == "report":
                    operation.assert_called_once_with(engine)
                else:
                    operation.assert_called_once_with(
                        engine,
                        expected_report_sha256="a" * 64,
                    )
                engine.dispose.assert_called_once_with()

    def test_postgres_engine_is_disposed_when_operation_fails(self) -> None:
        dsn_env = "CP7_POSTGRES_VALIDATION_DSN"
        dsn = "postgresql+psycopg://operator"
        config = SimpleNamespace(
            backend=migration_script.StatePlatformBackend.POSTGRESQL,
            dsn=dsn,
        )
        cases = (
            (
                "report",
                run_report,
                Namespace(
                    report=True,
                    expected_report_sha=None,
                    database_path=None,
                    dsn_env=dsn_env,
                ),
                "inspect_postgres_dispatch_aggregate",
            ),
            (
                "apply",
                run_apply,
                Namespace(
                    apply=True,
                    expected_report_sha="a" * 64,
                    database_path=None,
                    dsn_env=dsn_env,
                ),
                "apply_postgres_dispatch_aggregate",
            ),
        )

        for mode, runner, args, operation_name in cases:
            with self.subTest(mode=mode):
                engine = Mock()
                with (
                    patch.dict(os.environ, {dsn_env: dsn}, clear=True),
                    patch(
                        "src.storage.postgres.create_postgres_engine",
                        return_value=engine,
                    ),
                    patch.object(
                        migration_script,
                        "build_state_platform_runtime_config",
                        return_value=config,
                    ),
                    patch.object(
                        migration_script,
                        operation_name,
                        side_effect=RuntimeError("injected_postgres_failure"),
                    ),
                    self.assertRaisesRegex(RuntimeError, "injected_postgres_failure"),
                ):
                    runner(args)
                engine.dispose.assert_called_once_with()

    def test_postgres_dsn_guards_fail_before_engine_creation(self) -> None:
        cases = (
            (
                "report_invalid_env",
                run_report,
                Namespace(
                    report=True,
                    expected_report_sha=None,
                    database_path=None,
                    dsn_env="invalid-name",
                ),
                {},
                "mcp_dispatch_aggregate_dsn_env_invalid",
            ),
            (
                "apply_missing_dsn",
                run_apply,
                Namespace(
                    apply=True,
                    expected_report_sha="a" * 64,
                    database_path=None,
                    dsn_env="CP7_POSTGRES_VALIDATION_DSN",
                ),
                {},
                "mcp_dispatch_aggregate_dsn_env_missing",
            ),
        )

        for mode, runner, args, environment, reason_code in cases:
            with self.subTest(mode=mode):
                with (
                    patch.dict(os.environ, environment, clear=True),
                    patch(
                        "src.storage.postgres.create_postgres_engine"
                    ) as create_engine,
                    self.assertRaisesRegex(
                        MCPDispatchAggregateMigrationError,
                        f"^{reason_code}$",
                    ),
                ):
                    runner(args)
                create_engine.assert_not_called()

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

    def test_apply_supported_business_rows_preserves_identity_and_adds_defaults(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "legacy.sqlite3"
            self._create_populated_supported_legacy_database(path)
            before = inspect_sqlite_dispatch_aggregate(path).as_payload()

            self.assertTrue(before["migration_required"])
            self.assertTrue(before["apply_eligible"])
            self.assertEqual(before["blocker_reason_codes"], [])

            applied = apply_sqlite_dispatch_aggregate(
                path,
                expected_report_sha256=str(before["report_sha256"]),
            ).as_payload()

            self.assertEqual(applied["result"], "applied")
            after = inspect_sqlite_dispatch_aggregate(path).as_payload()
            self.assertFalse(after["migration_required"])
            self.assertEqual(after["row_counts"]["mcp_call_record"], 1)
            self.assertEqual(
                after["row_counts"]["mcp_dispatch_resume_outbox"], 1
            )
            connection = sqlite3.connect(path)
            try:
                call = connection.execute(
                    "SELECT call_ref, status, may_have_dispatched, "
                    "pending_action_id, continuation_of_call_ref "
                    "FROM mcp_call_record"
                ).fetchone()
                outbox = connection.execute(
                    "SELECT outbox_id, status, resume_reason, resume_receipt_id, "
                    "resume_answer_id, selector_step_total, approval_round_total "
                    "FROM mcp_dispatch_resume_outbox"
                ).fetchone()
                integrity = connection.execute(
                    "PRAGMA integrity_check"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(
                call,
                ("private-call", "active", 1, None, None),
            )
            self.assertEqual(
                outbox,
                ("private-outbox", "claimed", "initial", None, None, 0, 0),
            )
            self.assertEqual(integrity, "ok")

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

    def test_business_row_swap_rolls_back_without_identity_loss(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "legacy.sqlite3"
            self._create_populated_supported_legacy_database(path)
            report_sha = str(
                inspect_sqlite_dispatch_aggregate(path).as_payload()[
                    "report_sha256"
                ]
            )
            from src.storage import mcp_dispatch_aggregate_migration as migration

            original_replace = migration._replace_sqlite_table_preserving_rows

            def fail_after_first_swap(connection, table_name):
                original_replace(connection, table_name)
                raise RuntimeError("injected_business_swap_failure")

            with patch.object(
                migration,
                "_replace_sqlite_table_preserving_rows",
                side_effect=fail_after_first_swap,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "injected_business_swap_failure",
                ):
                    apply_sqlite_dispatch_aggregate(
                        path,
                        expected_report_sha256=report_sha,
                    )

            rolled_back = inspect_sqlite_dispatch_aggregate(path).as_payload()
            self.assertTrue(rolled_back["migration_required"])
            self.assertTrue(rolled_back["apply_eligible"])
            connection = sqlite3.connect(path)
            try:
                call_ids = connection.execute(
                    "SELECT call_ref FROM mcp_call_record"
                ).fetchall()
                outbox_ids = connection.execute(
                    "SELECT outbox_id FROM mcp_dispatch_resume_outbox"
                ).fetchall()
                temporary_tables = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name LIKE '__maf_v6_%'"
                ).fetchall()
            finally:
                connection.close()
            self.assertEqual(call_ids, [("private-call",)])
            self.assertEqual(outbox_ids, [("private-outbox",)])
            self.assertEqual(temporary_tables, [])

            retried = apply_sqlite_dispatch_aggregate(
                path,
                expected_report_sha256=report_sha,
            )
            self.assertEqual(retried.result, "applied")

    def test_apply_unsupported_business_row_shape_is_authority_conflict(self) -> None:
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

    def test_cli_exit_codes_and_stdout_are_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "legacy.sqlite3"
            self._create_empty_legacy_database(path)
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = main(
                    [
                        "--apply",
                        "--database-path",
                        str(path),
                        "--expected-report-sha",
                        "0" * 64,
                    ]
                )
            self.assertEqual(exit_code, 2)
            self.assertEqual(
                output.getvalue(),
                '{"reason_code":"mcp_dispatch_aggregate_report_changed",'
                '"result":"rejected"}\n',
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
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = main(
                    [
                        "--apply",
                        "--database-path",
                        str(path),
                        "--expected-report-sha",
                        str(blocked["report_sha256"]).removeprefix("sha256:"),
                    ]
                )
            self.assertEqual(exit_code, 3)
            self.assertEqual(
                output.getvalue(),
                '{"reason_code":"mcp_call_record_business_rows_shape_unsupported",'
                '"result":"rejected"}\n',
            )

    def test_postgres_cutover_plan_uses_bounded_lock_and_not_valid_replacement(
        self,
    ) -> None:
        manifest = build_postgres_fresh_cutover_schema_manifest()
        inspection = SchemaInspection.from_manifest(manifest)
        tables = {
            name: dict(columns) for name, columns in inspection.tables.items()
        }
        for column_name in (
            "resume_reason",
            "resume_receipt_id",
            "resume_answer_id",
            "selector_step_total",
            "approval_round_total",
        ):
            tables["mcp_dispatch_resume_outbox"].pop(column_name)
        for column_name in (
            "pending_action_id",
            "continuation_of_call_ref",
        ):
            tables["mcp_call_record"].pop(column_name)
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
        self.assertIn(
            "ADD COLUMN resume_reason text NOT NULL DEFAULT 'initial'",
            sql,
        )
        self.assertIn(
            "ADD COLUMN selector_step_total bigint NOT NULL DEFAULT 0",
            sql,
        )
        self.assertIn(
            "ADD COLUMN approval_round_total bigint NOT NULL DEFAULT 0",
            sql,
        )
        self.assertIn("NOT VALID", sql)
        self.assertLess(
            sql.index("VALIDATE CONSTRAINT"),
            sql.index(f"DROP CONSTRAINT IF EXISTS {status_name}"),
        )
        self.assertTrue(plan.plan_sha256.startswith("sha256:"))
