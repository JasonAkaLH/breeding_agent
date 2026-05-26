from __future__ import annotations

import unittest
from dataclasses import dataclass

from src.storage.postgres.bootstrap import _column_type_name
from src.state.postgres.runtime_schema import build_postgres_fresh_cutover_schema_manifest
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
        self.assertFalse(plan.actions)
        self.assertFalse(plan.operator_only_actions)

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
