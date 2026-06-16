from __future__ import annotations

import dataclasses
import unittest

from sqlalchemy import inspect

from src.core.models import AuthUserToken
from src.state.postgres.runtime_schema import build_postgres_fresh_cutover_schema_manifest
from src.state.postgres.schema_reconciler import SchemaInspection, assert_no_forbidden_schema_sql, plan_postgres_schema_reconciliation
from tests.storage.support import SQLiteStorageTestCase


class AuthGenerationSchemaTest(SQLiteStorageTestCase):
    def test_auth_user_token_core_model_has_generation_fields(self) -> None:
        field_names = [field.name for field in dataclasses.fields(AuthUserToken)]
        self.assertIn("auth_generation", field_names)
        self.assertIn("auth_generation_updated_at", field_names)
        token = AuthUserToken(username="alice", api_token_hash="hash")
        self.assertEqual(token.auth_generation, 0)
        self.assertIsNone(token.auth_generation_updated_at)

    def test_sqlite_auth_user_token_schema_includes_generation_columns(self) -> None:
        columns = {column["name"] for column in inspect(self.engine).get_columns("auth_user_token")}
        self.assertIn("auth_generation", columns)
        self.assertIn("auth_generation_updated_at", columns)

    def test_postgres_runtime_schema_manifest_includes_auth_generation_columns(self) -> None:
        manifest = build_postgres_fresh_cutover_schema_manifest()
        columns = manifest.table_columns["auth_user_token"]
        self.assertEqual(columns["auth_generation"], "bigint")
        self.assertIn("timestamp", columns["auth_generation_updated_at"])

    def test_postgres_reconciler_adds_missing_generation_columns_without_drop(self) -> None:
        manifest = build_postgres_fresh_cutover_schema_manifest()
        inspection = SchemaInspection.from_manifest(manifest)
        columns = dict(inspection.tables["auth_user_token"])
        columns.pop("auth_generation")
        columns.pop("auth_generation_updated_at")
        plan = plan_postgres_schema_reconciliation(manifest, inspection.with_table_columns("auth_user_token", columns))
        sql = plan.sql_script()
        self.assertIn("ADD COLUMN IF NOT EXISTS auth_generation bigint NOT NULL DEFAULT 0", sql)
        self.assertIn("auth_generation_updated_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP", sql)
        assert_no_forbidden_schema_sql(sql)


if __name__ == "__main__":
    unittest.main()
