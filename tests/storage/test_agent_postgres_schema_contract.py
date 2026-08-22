from __future__ import annotations

import unittest
from pathlib import Path

from src.state.postgres.runtime_schema import (
    POSTGRES_RUNTIME_SCHEMA_VERSION,
    build_postgres_fresh_cutover_schema_manifest,
    build_runtime_table_schema_ddl,
)
from src.storage.postgres import PostgreSQLAgentRepository
from src.storage.sqlite import SQLiteAgentRepository


class AgentPostgresSchemaContractTest(unittest.TestCase):
    def test_manifest_and_ddl_include_additive_agent_tables_and_constraints(self) -> None:
        manifest = build_postgres_fresh_cutover_schema_manifest()
        self.assertEqual(POSTGRES_RUNTIME_SCHEMA_VERSION, "maf.postgresql_fresh_runtime_schema.v9")
        self.assertTrue({"agent_run", "agent_item", "agent_final_receipt"}.issubset(manifest.runtime_table_names))
        self.assertEqual(manifest.table_columns["agent_run"]["lease_expires_at"], "timestamp with time zone")
        self.assertIn("ck_agent_item_agent_item_kind", manifest.check_constraints["agent_item"])
        ddl = build_runtime_table_schema_ddl().lower()
        self.assertIn("create table if not exists agent_run", ddl)
        self.assertIn("create table if not exists agent_item", ddl)
        self.assertIn("jsonb", ddl)

    def test_postgres_repository_uses_the_same_atomic_contract_implementation(self) -> None:
        self.assertTrue(issubclass(PostgreSQLAgentRepository, SQLiteAgentRepository))

    def test_narrow_repository_does_not_reference_sensitive_authority_tables(self) -> None:
        source = (
            Path(__file__).resolve().parents[2]
            / "src"
            / "storage"
            / "sqlite"
            / "agent_repository.py"
        ).read_text(encoding="utf-8").lower()
        for forbidden in ("credential", "mcp_call_record", "mcp_terminal", "conversation_file_resource"):
            self.assertNotIn(forbidden, source)
