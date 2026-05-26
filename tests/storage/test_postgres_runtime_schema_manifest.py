from __future__ import annotations

import unittest

from src.storage.sqlite.base import SQLiteBase
from src.state.postgres.runtime_schema import (
    POSTGRES_RUNTIME_TABLES,
    build_postgres_fresh_cutover_schema_manifest,
    build_runtime_index_schema_ddl,
    build_runtime_schema_ddl,
    build_runtime_table_schema_ddl,
)


class PostgresRuntimeSchemaManifestTest(unittest.TestCase):
    def test_manifest_covers_all_sqlite_runtime_tables_and_state_tables(self) -> None:
        manifest = build_postgres_fresh_cutover_schema_manifest()
        sqlite_tables = set(SQLiteBase.metadata.tables)
        self.assertEqual(sqlite_tables, set(manifest.runtime_table_names))
        for table_name in POSTGRES_RUNTIME_TABLES:
            self.assertIn(table_name, manifest.runtime_table_names)
        for table_name in (
            "state_write_command",
            "state_partition_cursor",
            "state_write_dead_letter",
            "state_write_archive",
            "state_migration_ledger",
        ):
            self.assertIn(table_name, manifest.operational_table_names)
        self.assertTrue(manifest.checksum)

    def test_runtime_ddl_uses_jsonb_timestamptz_and_no_foreign_keys(self) -> None:
        ddl = build_runtime_schema_ddl()
        lowered = ddl.lower()
        self.assertIn("jsonb", lowered)
        self.assertIn("timestamp with time zone", lowered)
        self.assertNotIn("foreign key", lowered)
        self.assertNotIn("drop table", lowered)
        self.assertNotIn("truncate", lowered)

    def test_runtime_bootstrap_can_create_tables_before_indexes_for_existing_schema_reconciliation(self) -> None:
        table_ddl = build_runtime_table_schema_ddl().lower()
        index_ddl = build_runtime_index_schema_ddl().lower()
        self.assertIn("create table", table_ddl)
        self.assertNotIn("create index", table_ddl)
        self.assertIn("idx_conversation_delete_status_updated", index_ddl)
        self.assertIn("delete_phase", index_ddl)

    def test_manifest_checksum_changes_when_table_spec_changes(self) -> None:
        manifest = build_postgres_fresh_cutover_schema_manifest()
        mutated = manifest.with_runtime_table_names((*manifest.runtime_table_names, "extra_table"))
        self.assertNotEqual(manifest.checksum, mutated.checksum)
