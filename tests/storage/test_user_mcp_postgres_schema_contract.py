from __future__ import annotations

import inspect
import unittest

from src.state.postgres.runtime_schema import build_postgres_fresh_cutover_schema_manifest, build_runtime_index_schema_ddl
from src.storage.postgres.repositories import PostgreSQLStorage


class UserMCPPostgresSchemaContractTest(unittest.TestCase):
    def test_manifest_contains_coordination_tables_without_runtime_payload_columns(self) -> None:
        manifest = build_postgres_fresh_cutover_schema_manifest()
        expected = {
            "user_mcp_server",
            "user_mcp_tool_grant",
            "user_mcp_health_attempt",
            "user_mcp_scope_lease",
            "mcp_credential_key_validation",
            "mcp_branch_record",
            "mcp_call_record",
            "mcp_remote_task_binding",
            "mcp_sealed_state",
            "mcp_connection_lease",
            "mcp_audit_event",
        }
        self.assertTrue(expected.issubset(manifest.runtime_table_names))
        forbidden = {"tool_list", "input_schema", "output_schema", "session_id", "remote_task_id", "result"}
        for table_name in expected:
            self.assertFalse(forbidden.intersection(manifest.table_columns[table_name]))
        ddl = build_runtime_index_schema_ddl()
        self.assertIn("idx_user_mcp_server_owner_server", ddl)
        self.assertIn("idx_user_mcp_health_attempt_lease", ddl)
        self.assertIn("idx_user_mcp_scope_lease_expiry", ddl)
        self.assertIn("idx_mcp_call_owner_task", ddl)
        self.assertIn("idx_mcp_remote_task_poll", ddl)
        self.assertIn("idx_mcp_audit_expiry", ddl)

    def test_postgres_hotspots_use_explicit_row_locks(self) -> None:
        source = inspect.getsource(PostgreSQLStorage)
        self.assertIn("with_for_update", source)
        for method in (
            "update_user_mcp_server", "claim_user_mcp_health_attempt", "renew_user_mcp_health_attempt",
            "complete_user_mcp_health_attempt", "acquire_user_mcp_scope_lease", "renew_user_mcp_scope_lease",
            "release_user_mcp_health_attempt", "mark_user_mcp_server_deleted", "finalize_user_mcp_server_delete",
            "reserve_mcp_call",
        ):
            self.assertIn(f"def {method}", source)
