from __future__ import annotations

from pathlib import Path
import re
import unittest


SQL_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts/postgres/user_mcp_legacy_migration_permissions.sql"
)
SQL = SQL_PATH.read_text(encoding="utf-8")
NORMALIZED_SQL = " ".join(SQL.split())


class UserMCPLegacyMigrationPostgresPermissionsTest(unittest.TestCase):
    def test_roles_are_nonlogin_and_separate_from_rollout_authority(self) -> None:
        self.assertNotIn("mcp_rollout_api", SQL)
        self.assertIn("CREATE ROLE maf_mcp_legacy_migrator NOLOGIN", SQL)
        self.assertIn("CREATE ROLE maf_mcp_migration_api_owner NOLOGIN", SQL)
        self.assertIn(
            "CREATE SCHEMA IF NOT EXISTS mcp_migration_api",
            SQL,
        )
        self.assertNotRegex(SQL, r"(?i)\bPASSWORD\b")

    def test_scalar_definer_function_is_fixed_and_closed(self) -> None:
        definition = self._function_definition("apply_legacy_migration_candidate")
        self.assertIn("RETURNS pg_catalog.bool", definition)
        self.assertIn("SECURITY DEFINER", definition)
        self.assertIn("SET search_path = pg_catalog", definition)
        self.assertNotIn("p_event_type", definition)
        self.assertNotIn("p_disposition", definition)
        self.assertIn("'mcp.legacy.config_migrated'", definition)
        self.assertIn("'migrate_owner'", definition)
        self.assertIn("p_target_server_id IS DISTINCT FROM p_server_id", definition)
        self.assertIn("p_credential_ciphertext IS NULL", definition)
        self.assertIn("p_credential_ciphertext IS NOT NULL", definition)
        self.assertIn("credential_storage_digest", definition)
        self.assertIn("pg_catalog.sha256", definition)
        self.assertIn("p_auth_metadata #>> ARRAY[", definition)
        self.assertEqual(definition.count("^sha256:[0-9a-f]{64}$"), 8)
        self.assertEqual(definition.count("^hmac-sha256:[0-9a-f]{64}$"), 2)
        self.assertIn(
            "pg_catalog.statement_timestamp() > p_evidence_expires_at", definition
        )

    def test_replay_snapshot_returns_only_nonsecret_credential_binding(self) -> None:
        definition = self._function_definition(
            "read_legacy_migration_replay_snapshot"
        )
        self.assertIn("- 'credential_ciphertext' - 'credential_nonce'", definition)
        self.assertIn("'credential_storage_digest'", definition)
        self.assertIn("pg_catalog.sha256", definition)

    def test_all_three_record_identities_are_advisory_locked_and_compared(self) -> None:
        definition = self._function_definition("apply_legacy_migration_candidate")
        self.assertIn("'migration:' || p_migration_id", definition)
        self.assertIn("'plan_source:' || p_plan_fingerprint", definition)
        self.assertIn("'target:' || p_target_server_id", definition)
        self.assertIn("ORDER BY identity", definition)
        self.assertIn("WHERE migration_id = p_migration_id;", definition)
        self.assertIn("WHERE plan_fingerprint = p_plan_fingerprint", definition)
        self.assertIn("AND source_server_id = p_source_server_id;", definition)
        self.assertIn("WHERE target_server_id = p_target_server_id;", definition)
        self.assertNotIn("FOR UPDATE", definition)
        self.assertIn("RETURN server_missing OR record_missing", definition)

    def test_batch_lock_function_sorts_the_complete_identity_set(self) -> None:
        definition = self._function_definition("lock_legacy_migration_batch")
        self.assertIn("RETURNS pg_catalog.void", definition)
        self.assertIn("SECURITY DEFINER", definition)
        self.assertIn("SET search_path = pg_catalog", definition)
        self.assertIn("SELECT DISTINCT identity", definition)
        self.assertIn("ORDER BY identity", definition)
        self.assertIn("pg_catalog.pg_advisory_xact_lock", definition)
        self.assertNotIn("user_mcp_server", definition)

    def test_replay_snapshot_is_identity_scoped_and_never_returns_ciphertext(
        self,
    ) -> None:
        definition = self._function_definition(
            "read_legacy_migration_replay_snapshot"
        )
        self.assertIn("RETURNS pg_catalog.jsonb", definition)
        self.assertIn("SECURITY DEFINER", definition)
        self.assertIn("SET search_path = pg_catalog", definition)
        self.assertIn("'credential_ciphertext'", definition)
        self.assertIn("'credential_nonce'", definition)
        self.assertIn("'status', 'conflict'", definition)
        self.assertIn("'status', 'exact'", definition)

    def test_login_has_no_direct_base_table_access_and_only_closed_functions(
        self,
    ) -> None:
        self.assertIn(
            "FROM PUBLIC, maf_mcp_legacy_migrator, maf_mcp_migration_api_owner",
            NORMALIZED_SQL,
        )
        self.assertNotRegex(
            NORMALIZED_SQL,
            r"GRANT (?:SELECT|INSERT|UPDATE|DELETE)[^;]+TO maf_mcp_legacy_migrator",
        )
        grants = re.findall(
            r"GRANT EXECUTE ON FUNCTION ([^;]+) TO maf_mcp_legacy_migrator;",
            NORMALIZED_SQL,
        )
        self.assertEqual(
            {grant.split("(", 1)[0] for grant in grants},
            {
                "mcp_migration_api.apply_legacy_migration_candidate",
                "mcp_migration_api.lock_legacy_migration_batch",
                "mcp_migration_api.read_legacy_migration_replay_snapshot",
            },
        )
        self.assertIn(
            "REVOKE SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, "
            "TRIGGER ON TABLE",
            NORMALIZED_SQL,
        )

    def test_record_is_append_only_and_functions_are_owner_controlled(self) -> None:
        self.assertIn(
            "BEFORE UPDATE OR DELETE ON public.mcp_legacy_migration_record",
            NORMALIZED_SQL,
        )
        self.assertIn(
            ") OWNER TO maf_mcp_migration_api_owner",
            NORMALIZED_SQL,
        )
        self.assertIn(
            "REVOKE ALL ON ALL FUNCTIONS IN SCHEMA mcp_migration_api FROM PUBLIC",
            NORMALIZED_SQL,
        )

    @staticmethod
    def _function_definition(name: str) -> str:
        match = re.search(
            rf"CREATE OR REPLACE FUNCTION mcp_migration_api\.{name}\(.+?\$function\$;",
            SQL,
            flags=re.DOTALL,
        )
        if match is None:
            raise AssertionError(f"function definition missing: {name}")
        return match.group(0)


if __name__ == "__main__":
    unittest.main()
