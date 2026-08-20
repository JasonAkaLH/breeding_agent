from __future__ import annotations

import unittest
from contextlib import contextmanager
from dataclasses import dataclass
from unittest.mock import patch

from src.storage.postgres.bootstrap import (
    _column_type_name,
    _inspect_current_schema,
    bootstrap_postgres_database,
)
from src.state.postgres.runtime_schema import (
    POSTGRES_CP7_TRIGGER_NAMES,
    build_postgres_fresh_cutover_schema_manifest,
)
from src.state.postgres.schema_reconciler import (
    ForbiddenPostgresSchemaActionError,
    PostgresSchemaDriftError,
    SchemaInspection,
    assert_no_forbidden_schema_sql,
    plan_postgres_schema_reconciliation,
)


class PostgresSchemaReconcilerTest(unittest.TestCase):
    def test_empty_db_plan_creates_objects_without_destructive_sql(self) -> None:
        manifest = build_postgres_fresh_cutover_schema_manifest()
        plan = plan_postgres_schema_reconciliation(manifest, SchemaInspection.empty())
        self.assertTrue(plan.actions)
        self.assertEqual(plan.operator_only_actions, ())
        sql = plan.sql_script()
        self.assertIn("CREATE TABLE", sql)
        assert_no_forbidden_schema_sql(sql)

    def test_matching_schema_is_noop(self) -> None:
        manifest = build_postgres_fresh_cutover_schema_manifest()
        inspection = SchemaInspection.from_manifest(manifest)
        plan = plan_postgres_schema_reconciliation(manifest, inspection)
        self.assertEqual(
            [action.kind for action in plan.actions],
            ["backfill_mcp_remote_task_publication"],
        )
        self.assertFalse(plan.operator_only_actions)

    def test_remote_task_publication_column_adds_and_backfills_only_proven_rows(self) -> None:
        manifest = build_postgres_fresh_cutover_schema_manifest()
        inspection = SchemaInspection.from_manifest(manifest)
        columns = dict(inspection.tables["mcp_remote_task_binding"])
        columns.pop("published_at")
        inspection = inspection.with_table_columns("mcp_remote_task_binding", columns)

        sql = plan_postgres_schema_reconciliation(manifest, inspection).sql_script()

        self.assertIn(
            "ALTER TABLE mcp_remote_task_binding ADD COLUMN published_at", sql
        )
        self.assertIn("SET published_at = COALESCE(next_poll_at, terminal_at, updated_at)", sql)
        self.assertIn("next_poll_at IS NOT NULL OR terminal_at IS NOT NULL", sql)

    def test_missing_additive_column_is_safe_alter(self) -> None:
        manifest = build_postgres_fresh_cutover_schema_manifest()
        inspection = SchemaInspection.from_manifest(manifest)
        columns = dict(inspection.tables["conversation"])
        columns.pop("title")
        inspection = inspection.with_table_columns("conversation", columns)
        plan = plan_postgres_schema_reconciliation(manifest, inspection)
        sql = plan.sql_script()
        self.assertIn("ALTER TABLE conversation ADD COLUMN title", sql)
        assert_no_forbidden_schema_sql(sql)

    def test_incompatible_column_type_fails_closed_without_apply_plan(self) -> None:
        manifest = build_postgres_fresh_cutover_schema_manifest()
        inspection = SchemaInspection.from_manifest(manifest)
        columns = dict(inspection.tables["conversation"])
        columns["status"] = "integer"
        inspection = inspection.with_table_columns("conversation", columns)
        with self.assertRaises(PostgresSchemaDriftError):
            plan_postgres_schema_reconciliation(manifest, inspection)

    def test_forbidden_sql_guard_rejects_destructive_tokens(self) -> None:
        with self.assertRaises(ForbiddenPostgresSchemaActionError):
            assert_no_forbidden_schema_sql("DROP TABLE conversation")
        with self.assertRaises(ForbiddenPostgresSchemaActionError):
            assert_no_forbidden_schema_sql("ALTER TABLE conversation RENAME TO old_conversation")
        with self.assertRaises(ForbiddenPostgresSchemaActionError):
            assert_no_forbidden_schema_sql("DELETE FROM conversation")

    def test_bootstrap_preserves_inspected_timestamp_timezone_metadata(self) -> None:
        @dataclass
        class FakeTimestampType:
            timezone: bool

            def __str__(self) -> str:
                return "TIMESTAMP"

        self.assertEqual(
            _column_type_name({"type": FakeTimestampType(timezone=True)}),
            "timestamp with time zone",
        )
        self.assertEqual(
            _column_type_name({"type": FakeTimestampType(timezone=False)}),
            "timestamp",
        )

    def test_replaces_legacy_task_route_check_without_rewriting_rows(self) -> None:
        manifest = build_postgres_fresh_cutover_schema_manifest()
        inspection = SchemaInspection.from_manifest(manifest)
        checks = {
            table: dict(constraints)
            for table, constraints in inspection.check_constraints.items()
        }
        checks["task"].pop("ck_task_task_mcp_route_reason_code")
        checks["task"]["task_mcp_route_reason_code"] = (
            "mcp_route_reason_code IS NULL OR mcp_route_reason_code IN "
            "('routing_off', 'shadow_enabled', 'enforce_selected', "
            "'cohort_not_selected', 'percent_not_selected', 'no_execution_path')"
        )
        inspection = SchemaInspection(
            tables=inspection.tables,
            enum_types=inspection.enum_types,
            check_constraints=checks,
            triggers=inspection.triggers,
        )

        plan = plan_postgres_schema_reconciliation(manifest, inspection)
        sql = plan.sql_script()

        self.assertIn(
            "DROP CONSTRAINT IF EXISTS task_mcp_route_reason_code", sql
        )
        self.assertIn("no_user_scoped_server", sql)
        self.assertIn("ADD CONSTRAINT ck_task_task_mcp_route_reason_code", sql)
        self.assertIn("VALIDATE CONSTRAINT ck_task_task_mcp_route_reason_code", sql)
        self.assertNotIn("UPDATE task", sql)
        self.assertNotIn("DELETE FROM task", sql)

    def test_additive_plan_installs_missing_cp7_mutation_triggers(self) -> None:
        manifest = build_postgres_fresh_cutover_schema_manifest()
        inspection = SchemaInspection.from_manifest(manifest)
        inspection = SchemaInspection(
            tables=inspection.tables,
            enum_types=inspection.enum_types,
            check_constraints=inspection.check_constraints,
            triggers=(),
        )

        sql = plan_postgres_schema_reconciliation(manifest, inspection).sql_script()

        self.assertIn("maf_reject_append_only_mutation", sql)
        self.assertIn("trg_mcp_cp7_safety_ledger_append_only", sql)
        self.assertIn("trg_mcp_cp7_candidate_guard_monotonic", sql)

    def test_reconciles_missing_drifted_and_unknown_cp7_checks(self) -> None:
        manifest = build_postgres_fresh_cutover_schema_manifest()
        inspection = SchemaInspection.from_manifest(manifest)
        checks = {
            table: dict(constraints)
            for table, constraints in inspection.check_constraints.items()
        }
        missing_name = "ck_mcp_no_server_intent_mcp_no_server_intent_revision"
        checks["mcp_no_server_intent"].pop(missing_name)
        drifted_name = "ck_mcp_cp7_safety_ledger_mcp_cp7_safety_gap_reason"
        checks["mcp_cp7_safety_ledger"][drifted_name] = "record_kind <> 'gap'"
        checks["mcp_cp7_candidate_guard"]["hostile_extra_check"] = (
            "invalid_latched = false"
        )
        inspection = SchemaInspection(
            tables=inspection.tables,
            enum_types=inspection.enum_types,
            check_constraints=checks,
            triggers=inspection.triggers,
        )

        sql = plan_postgres_schema_reconciliation(manifest, inspection).sql_script()

        self.assertIn(f"ADD CONSTRAINT {missing_name}", sql)
        self.assertIn(f"DROP CONSTRAINT IF EXISTS {drifted_name}", sql)
        self.assertIn(f"ADD CONSTRAINT {drifted_name}", sql)
        self.assertIn(f"VALIDATE CONSTRAINT {drifted_name}", sql)
        self.assertIn("DROP CONSTRAINT IF EXISTS hostile_extra_check", sql)

    def test_legacy_dispatch_outbox_requires_controlled_operator_cutover(self) -> None:
        manifest = build_postgres_fresh_cutover_schema_manifest()
        inspection = SchemaInspection.from_manifest(manifest)
        tables = {
            name: dict(columns) for name, columns in inspection.tables.items()
        }
        for column in (
            "resume_reason",
            "resume_receipt_id",
            "resume_answer_id",
            "selector_step_total",
            "approval_round_total",
        ):
            tables["mcp_dispatch_resume_outbox"].pop(column)
        checks = {
            table: dict(constraints)
            for table, constraints in inspection.check_constraints.items()
        }
        checks["mcp_dispatch_resume_outbox"] = {
            "ck_mcp_dispatch_resume_outbox_mcp_dispatch_resume_status": (
                "status IN ('pending', 'claimed', 'completed', 'aborted')"
            )
        }
        inspection = SchemaInspection(
            tables=tables,
            enum_types=inspection.enum_types,
            check_constraints=checks,
            triggers=inspection.triggers,
        )

        plan = plan_postgres_schema_reconciliation(manifest, inspection)

        self.assertIn(
            "mcp_dispatch_aggregate_cutover_required:mcp_dispatch_resume_outbox",
            plan.operator_only_actions,
        )
        self.assertNotIn(
            "ALTER TABLE mcp_dispatch_resume_outbox",
            plan.sql_script(),
        )

    def test_result_authority_columns_are_safe_additive_reconciliation(self) -> None:
        manifest = build_postgres_fresh_cutover_schema_manifest()
        inspection = SchemaInspection.from_manifest(manifest)
        tables = {
            name: dict(columns) for name, columns in inspection.tables.items()
        }
        for column in (
            "output_schema",
            "output_schema_sha256",
            "terminal_result_source",
        ):
            tables["mcp_call_record"].pop(column)
        for column in (
            "result_parser_revision",
            "validated_checkpoint_sha256",
            "parsed_model_sha256",
        ):
            tables["mcp_terminal_result_receipt"].pop(column)
        legacy = SchemaInspection(
            tables=tables,
            enum_types=inspection.enum_types,
            check_constraints=inspection.check_constraints,
            triggers=inspection.triggers,
        )

        plan = plan_postgres_schema_reconciliation(manifest, legacy)
        sql = plan.sql_script()

        self.assertFalse(plan.operator_only_actions)
        self.assertIn(
            "ALTER TABLE mcp_call_record ADD COLUMN output_schema jsonb", sql
        )
        self.assertIn(
            "ALTER TABLE mcp_terminal_result_receipt ADD COLUMN result_parser_revision text",
            sql,
        )

    def test_result_parser_metric_names_replace_only_the_metric_name_constraint(self) -> None:
        manifest = build_postgres_fresh_cutover_schema_manifest()
        inspection = SchemaInspection.from_manifest(manifest)
        checks = {
            table: dict(constraints)
            for table, constraints in inspection.check_constraints.items()
        }
        constraint_name = next(
            name
            for name in checks["mcp_rollout_metric_bucket"]
            if name.endswith("mcp_rollout_metric_name")
        )
        checks["mcp_rollout_metric_bucket"][constraint_name] = (
            checks["mcp_rollout_metric_bucket"][constraint_name]
            .replace(", 'mcp_result_parser_outcomes_total'", "")
            .replace(", 'mcp_result_parser_duration_seconds'", "")
        )
        legacy = SchemaInspection(
            tables=inspection.tables,
            enum_types=inspection.enum_types,
            check_constraints=checks,
            triggers=inspection.triggers,
        )

        plan = plan_postgres_schema_reconciliation(manifest, legacy)
        sql = plan.sql_script()

        self.assertFalse(plan.operator_only_actions)
        self.assertIn(
            f"DROP CONSTRAINT IF EXISTS {constraint_name}", sql
        )
        self.assertIn("mcp_result_parser_outcomes_total", sql)
        self.assertIn("mcp_result_parser_duration_seconds", sql)
        self.assertIn(f"VALIDATE CONSTRAINT {constraint_name}", sql)

    def test_postgres_bootstrap_rejects_operator_cutover_before_schema_mutation(
        self,
    ) -> None:
        manifest = build_postgres_fresh_cutover_schema_manifest()
        inspection = SchemaInspection.from_manifest(manifest)
        tables = {
            name: dict(columns) for name, columns in inspection.tables.items()
        }
        tables["mcp_dispatch_resume_outbox"].pop("resume_reason")
        legacy = SchemaInspection(
            tables=tables,
            enum_types=inspection.enum_types,
            check_constraints=inspection.check_constraints,
            triggers=inspection.triggers,
        )

        class FakeConnection:
            def __init__(self) -> None:
                self.statements: list[str] = []

            def execute(self, statement):
                self.statements.append(str(statement))
                return object()

        class FakeEngine:
            def __init__(self) -> None:
                self.connection = FakeConnection()

            @contextmanager
            def begin(self):
                yield self.connection

        engine = FakeEngine()
        with patch(
            "src.storage.postgres.bootstrap._inspect_current_schema",
            return_value=legacy,
        ), self.assertRaisesRegex(
            PostgresSchemaDriftError,
            "mcp_dispatch_aggregate_migration_required",
        ):
            bootstrap_postgres_database(engine)

        self.assertFalse(
            any("CREATE TABLE" in statement for statement in engine.connection.statements)
        )

    def test_trigger_inspection_covers_every_manifested_cp7_trigger(self) -> None:
        class FakeInspector:
            def get_table_names(self) -> list[str]:
                return []

        class FakeResult:
            def __init__(self, rows: list[tuple[str]]) -> None:
                self._rows = rows

            def all(self) -> list[tuple[str]]:
                return self._rows

        class FakeConnection:
            def execute(self, statement):
                sql = str(statement)
                if "FROM pg_type" in sql:
                    return FakeResult([("state_command_status",)])
                return FakeResult(
                    [(name,) for name in POSTGRES_CP7_TRIGGER_NAMES]
                    + [("unrelated_trigger",)]
                )

        with patch(
            "src.storage.postgres.bootstrap.inspect", return_value=FakeInspector()
        ):
            inspection = _inspect_current_schema(FakeConnection())

        self.assertEqual(set(inspection.triggers), set(POSTGRES_CP7_TRIGGER_NAMES))
