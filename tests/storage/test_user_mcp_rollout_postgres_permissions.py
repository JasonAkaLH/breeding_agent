from __future__ import annotations

import os
from pathlib import Path
import re
import unittest

from src.integrations.mcp.shadow_evidence import (
    MCP_SHADOW_SAMPLE_CLOSED_VALUES,
    MCP_SHADOW_SAMPLE_EXPECTATIONS,
)
from src.integrations.mcp.rollout_evidence import CURRENT_MCP_SHADOW_SCENARIOS
from src.state.postgres.runtime_schema import build_runtime_table_schema_ddl


REPO_ROOT = Path(__file__).resolve().parents[2]
PERMISSIONS_SQL_PATH = REPO_ROOT / "scripts/postgres/user_mcp_rollout_permissions.sql"
PERMISSIONS_SQL = PERMISSIONS_SQL_PATH.read_text(encoding="utf-8")
NORMALIZED_SQL = " ".join(PERMISSIONS_SQL.split())

ROLLOUT_TABLES = (
    "mcp_rollout_gate_scope",
    "mcp_rollout_metric_bucket",
    "mcp_shadow_audit_sample",
    "mcp_rollout_evidence_snapshot",
    "mcp_rollout_stage_approval",
    "mcp_rollout_deployment_activation",
    "mcp_rollout_promotion_block",
    "mcp_rollout_block_resolution",
    "mcp_rollout_instance_config",
    "mcp_rollout_drill_observation",
)
HISTORY_TABLES = (
    "mcp_rollout_drill_observation",
    "mcp_rollout_evidence_snapshot",
    "mcp_rollout_stage_approval",
    "mcp_rollout_deployment_activation",
    "mcp_rollout_promotion_block",
    "mcp_rollout_block_resolution",
)
ROLES = (
    "maf_rollout_app_writer",
    "maf_rollout_snapshot_producer",
    "maf_rollout_ci_evidence_writer",
    "maf_rollout_gate_evaluator",
    "maf_rollout_operator",
    "maf_rollout_validator",
    "maf_rollout_drill_recorder",
)
API_OWNER = "maf_rollout_api_owner"


class UserMCPRolloutPostgresPermissionsContractTest(unittest.TestCase):
    def test_template_is_additive_idempotent_and_contains_no_login_secrets(
        self,
    ) -> None:
        self.assertTrue(PERMISSIONS_SQL_PATH.is_file())
        self.assertNotRegex(PERMISSIONS_SQL, r"(?i)\bPASSWORD\b")
        self.assertNotRegex(
            PERMISSIONS_SQL,
            r"(?i)(?:CREATE|ALTER)\s+ROLE\s+\S+\s+LOGIN\b",
        )
        for role in ROLES:
            self.assertIn(
                f"IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = '{role}')",
                PERMISSIONS_SQL,
            )
            self.assertIn(f"CREATE ROLE {role} NOLOGIN", PERMISSIONS_SQL)
            self.assertIn(f"ALTER ROLE {role} NOLOGIN NOSUPERUSER", PERMISSIONS_SQL)
        self.assertIn(f"CREATE ROLE {API_OWNER} NOLOGIN", PERMISSIONS_SQL)
        self.assertIn(
            f"ALTER ROLE {API_OWNER} NOLOGIN NOINHERIT NOSUPERUSER",
            PERMISSIONS_SQL,
        )
        self.assertIn("CREATE SCHEMA IF NOT EXISTS mcp_rollout_api", PERMISSIONS_SQL)
        self.assertNotIn("DROP ROLE", PERMISSIONS_SQL.upper())
        self.assertNotIn("DROP TABLE", PERMISSIONS_SQL.upper())
        self.assertNotIn("DROP FUNCTION", PERMISSIONS_SQL.upper())

    def test_public_and_roles_have_no_base_table_dml(self) -> None:
        self.assertIn("FROM PUBLIC", NORMALIZED_SQL)
        self.assertIn(
            "REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE",
            NORMALIZED_SQL,
        )
        for table in ROLLOUT_TABLES:
            self.assertIn(f"public.{table}", PERMISSIONS_SQL)
        for privileges, recipient in re.findall(
            r"GRANT\s+([^;]+?)\s+TO\s+([^;]+?)\s*;",
            PERMISSIONS_SQL,
            flags=re.IGNORECASE | re.DOTALL,
        ):
            if re.search(r"(?i)\b(?:INSERT|UPDATE|DELETE)\b", privileges):
                self.assertEqual(recipient.strip(), API_OWNER)

    def test_security_definer_write_api_has_fixed_search_path(self) -> None:
        functions = (
            "upsert_metric_bucket",
            "set_metric_bucket",
            "append_shadow_audit_sample",
            "delete_expired_shadow_audit_samples",
            "upsert_instance_config_lease",
            "append_production_evidence_snapshot",
            "derive_production_evidence_snapshot",
            "prepare_production_evidence_snapshot",
            "finalize_production_evidence_snapshot",
            "append_ci_evidence_snapshot",
            "ensure_gate_scope",
            "append_stage_approval",
            "append_promotion_block",
            "append_deployment_activation",
            "append_block_resolution",
            "append_drill_observation",
        )
        for function_name in functions:
            definition = _function_definition(function_name)
            self.assertIn("SECURITY DEFINER", definition)
            self.assertIn("SET search_path = pg_catalog", definition)
        self.assertIn(
            "REVOKE ALL ON ALL FUNCTIONS IN SCHEMA mcp_rollout_api FROM PUBLIC",
            NORMALIZED_SQL,
        )

    def test_each_writer_role_receives_only_its_named_function_boundary(self) -> None:
        expected_grants = {
            "upsert_metric_bucket": "maf_rollout_app_writer",
            "set_metric_bucket": "maf_rollout_app_writer",
            "append_shadow_audit_sample": "maf_rollout_app_writer",
            "delete_expired_shadow_audit_samples": "maf_rollout_app_writer",
            "upsert_instance_config_lease": "maf_rollout_app_writer",
            "prepare_production_evidence_snapshot": "maf_rollout_snapshot_producer",
            "finalize_production_evidence_snapshot": "maf_rollout_snapshot_producer",
            "append_ci_evidence_snapshot": "maf_rollout_ci_evidence_writer",
            "append_stage_approval": "maf_rollout_operator",
            "append_promotion_block": "maf_rollout_gate_evaluator",
            "append_deployment_activation": "maf_rollout_operator",
            "append_block_resolution": "maf_rollout_operator",
            "append_drill_observation": "maf_rollout_drill_recorder",
        }
        for function_name, role in expected_grants.items():
            grant = _function_grant(function_name)
            self.assertRegex(grant, rf"\)\s+TO\s+{role}\s*;")

        validator_grants = re.findall(
            r"GRANT\s+([^;]+?)\s+TO\s+maf_rollout_validator\s*;",
            PERMISSIONS_SQL,
            flags=re.IGNORECASE | re.DOTALL,
        )
        self.assertEqual(len(validator_grants), 1)
        self.assertTrue(
            validator_grants[0].lstrip().upper().startswith("SELECT ON TABLE")
        )

    def test_ci_evidence_role_cannot_author_production_evidence(self) -> None:
        disabled_definition = _function_definition(
            "append_production_evidence_snapshot"
        )
        production_definition = _function_definition(
            "derive_production_evidence_snapshot"
        )
        ci_definition = _function_definition("append_ci_evidence_snapshot")
        self.assertIn(
            "'production_snapshot_producer', 'production'", production_definition
        )
        self.assertIn(
            "caller-authored production evidence is disabled", disabled_definition
        )
        self.assertIn("'ci_pipeline', 'ci'", ci_definition)
        finalize_definition = _function_definition(
            "finalize_production_evidence_snapshot"
        )
        self.assertIn("p_attestation_key_id", finalize_definition)
        self.assertIn("p_attestation_signature", finalize_definition)
        self.assertIn("^[0-9a-f]{64}$", finalize_definition)
        self.assertNotIn("p_attestation_key_id", ci_definition)
        self.assertNotIn(
            "maf_rollout_ci_evidence_writer",
            _function_grant("finalize_production_evidence_snapshot"),
        )

    def test_snapshot_producer_reads_only_deidentified_inputs(self) -> None:
        self.assertIn(
            "public.mcp_shadow_audit_sample, public.mcp_rollout_metric_bucket, public.mcp_rollout_drill_observation TO maf_rollout_snapshot_producer",
            NORMALIZED_SQL,
        )
        self.assertNotRegex(
            NORMALIZED_SQL,
            r"GRANT\s+(?:INSERT|UPDATE|DELETE)[^;]*TO\s+maf_rollout_snapshot_producer",
        )
        self.assertIn(
            "to_regprocedure",
            PERMISSIONS_SQL,
        )
        revoked = re.search(
            r"REVOKE EXECUTE ON FUNCTION mcp_rollout_api\.append_production_evidence_snapshot\(.+?\) FROM (.+?);",
            NORMALIZED_SQL,
        )
        self.assertIsNotNone(revoked)
        assert revoked is not None
        self.assertIn("maf_rollout_snapshot_producer", revoked.group(1))
        self.assertIn("maf_rollout_ci_evidence_writer", revoked.group(1))

    def test_server_derives_production_materialization_and_sample_digest(self) -> None:
        derive = _function_definition("derive_production_evidence_snapshot")
        finalize = _function_definition("finalize_production_evidence_snapshot")
        sample = _function_definition("append_shadow_audit_sample")
        self.assertIn("FROM public.mcp_shadow_audit_sample", derive)
        self.assertIn("FROM public.mcp_rollout_metric_bucket", derive)
        self.assertNotIn("p_payload pg_catalog.jsonb", derive)
        self.assertIn("derive_production_evidence_snapshot", finalize)
        self.assertIn("production evidence materialization changed", finalize)
        self.assertIn("expected_digest := pg_catalog.encode", sample)
        self.assertIn("p_payload_digest IS DISTINCT FROM expected_digest", sample)

    def test_materializer_derives_every_production_stage_from_exact_activation(
        self,
    ) -> None:
        derive = _function_definition("derive_production_evidence_snapshot")
        for stage in (
            "internal_shadow",
            "internal_enforce",
            "cohort_enforce",
            "full_enforce",
            "legacy_assembly_off",
        ):
            self.assertIn(f"'{stage}'", derive)
        self.assertIn("FROM public.mcp_rollout_deployment_activation", derive)
        self.assertIn("v_stage := v_activation.stage", derive)
        self.assertIn("v_config_fingerprint := v_activation.config_fingerprint", derive)
        self.assertIn("'kind', v_stage", derive)
        self.assertIn("FROM public.mcp_rollout_drill_observation", derive)

    def test_materializer_counts_only_exact_required_series_coverage(self) -> None:
        derive = _function_definition("derive_production_evidence_snapshot")
        for red_line in (
            "cross_user_access",
            "secret_exposure",
            "dual_tool_call",
            "unauthorized_tool_call",
            "endpoint_policy_bypass",
            "unknown_result_replay",
            "shadow_tool_call",
            "persistent_resource_leak",
        ):
            self.assertIn(f"('{red_line}')", derive)
        self.assertIn("pg_catalog.range_agg", derive)
        self.assertIn("@> pg_catalog.tstzrange", derive)
        self.assertIn("SELECT pg_catalog.count(*) FILTER (WHERE NOT covered)", derive)
        coverage = derive[derive.index("WITH required(kind, key1, key2) AS (") :]
        coverage = coverage[: coverage.index("WITH kind_list(call_kind) AS (")]
        self.assertIn("metric.metric_name = 'mcp_safety_red_line_total'", coverage)
        self.assertIn("metric.metric_name = 'mcp_tool_calls_total'", coverage)
        self.assertNotIn("mcp_route_requests_total", coverage)
        self.assertNotIn("mcp_tool_call_duration_seconds", coverage)

    def test_positive_red_line_is_atomic_and_only_ordinary_gauge_setter_skips_lock(
        self,
    ) -> None:
        metric = _function_definition("upsert_metric_bucket")
        setter = _function_definition("set_metric_bucket")
        self.assertIn("p_value IS NULL OR p_value < 0", metric)
        self.assertIn("one complete UTC-aligned minute", metric)
        self.assertIn("p_bucket_started_at AT TIME ZONE 'UTC'", metric)
        self.assertIn("p_bucket_ended_at <> p_bucket_started_at + INTERVAL '1 minute'", metric)
        self.assertIn(
            "p_metric_name = 'mcp_safety_red_line_total' AND p_value > 0", metric
        )
        self.assertIn("'safety_red_line_nonzero'", metric)
        self.assertIn("mcp_rollout_api.lock_gate_scope", metric)
        self.assertIn("safety red-line metric is additive-counter-only", setter)
        self.assertIn("one complete UTC-aligned minute", setter)
        self.assertNotIn("mcp_rollout_api.lock_gate_scope", setter)

    def test_shadow_sample_sql_matches_python_closed_value_contract(self) -> None:
        sample = _function_definition("append_shadow_audit_sample")
        for sql_parameter, contract_key in (
            ("scenario", "scenarios"),
            ("legacy_outcome", "outcomes"),
            ("shadow_outcome", "outcomes"),
            ("transport", "transports"),
            ("endpoint_policy", "endpoint_policies"),
            ("comparison", "comparisons"),
        ):
            self.assertEqual(
                _sql_closed_values(sample, f"p_{sql_parameter}"),
                MCP_SHADOW_SAMPLE_CLOSED_VALUES[contract_key],
            )
        self.assertEqual(
            _sql_closed_values(sample, "blocker.value #>> '{}'"),
            MCP_SHADOW_SAMPLE_CLOSED_VALUES["blockers"],
        )
        self.assertEqual(
            sample.count("^hmac-sha256:[0-9a-f]{64}$"),
            3,
        )
        self.assertEqual(
            _sql_matched_expectations(sample),
            MCP_SHADOW_SAMPLE_EXPECTATIONS,
        )
        for parameter in (
            "p_sample_id",
            "p_environment_id",
            "p_deployment_id",
            "p_config_fingerprint",
            "p_manifest_fingerprint",
            "p_fixture_fingerprint",
            "p_mapping_fingerprint",
            "p_scenario",
            "p_nonce",
            "p_legacy_outcome",
            "p_shadow_outcome",
            "p_transport",
            "p_endpoint_policy",
            "p_comparison",
            "p_blockers",
            "p_payload_digest",
            "p_observed_at",
            "p_recorded_at",
            "p_expires_at",
        ):
            self.assertIn(f"{parameter} IS NULL", sample)

    def test_snapshot_producer_uses_only_current_required_shadow_scenarios(self) -> None:
        producer = _function_definition("derive_production_evidence_snapshot")
        match = re.search(
            r"WITH scenario_list\(ordinality, scenario\) AS \(VALUES(.*?)\)\s*SELECT",
            producer,
            flags=re.DOTALL,
        )
        if match is None:
            raise AssertionError("current shadow scenario_list not found")
        scenarios = tuple(
            value
            for _ordinal, value in re.findall(
                r"\(\s*(\d+)\s*,\s*'([^']+)'\s*\)",
                match.group(1),
            )
        )
        self.assertEqual(
            scenarios,
            tuple(item.value for item in CURRENT_MCP_SHADOW_SCENARIOS),
        )
        self.assertNotIn("allowlisted_http_legacy_sse_success", scenarios)

    def test_history_tables_are_append_only(self) -> None:
        trigger_block = re.search(
            r"FOREACH ledger_table IN ARRAY ARRAY\[(.*?)\] LOOP",
            PERMISSIONS_SQL,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(trigger_block)
        assert trigger_block is not None
        for table in HISTORY_TABLES:
            self.assertIn(f"'{table}'", trigger_block.group(1))
            self.assertNotRegex(
                PERMISSIONS_SQL,
                rf"(?i)(UPDATE|DELETE\s+FROM)\s+public\.{re.escape(table)}\b",
            )
        self.assertIn("BEFORE UPDATE OR DELETE ON public.%I", PERMISSIONS_SQL)
        self.assertIn(
            "RAISE EXCEPTION 'rollout history is append-only'", PERMISSIONS_SQL
        )

    def test_security_definer_functions_have_a_constrained_owner(self) -> None:
        self.assertIn(
            "ALTER ROLE maf_rollout_api_owner NOLOGIN NOINHERIT NOSUPERUSER",
            PERMISSIONS_SQL,
        )
        self.assertIn(
            "ALTER FUNCTION %s OWNER TO maf_rollout_api_owner",
            PERMISSIONS_SQL,
        )
        self.assertIn(
            "REVOKE CREATE ON SCHEMA mcp_rollout_api FROM maf_rollout_api_owner",
            NORMALIZED_SQL,
        )
        owner_grants = re.findall(
            r"GRANT\s+([^;]+?)\s+TO\s+maf_rollout_api_owner\s*;",
            PERMISSIONS_SQL,
            flags=re.IGNORECASE | re.DOTALL,
        )
        self.assertEqual(len(owner_grants), 5)
        self.assertNotRegex(
            " ".join(owner_grants),
            r"(?i)\b(?:TRUNCATE|REFERENCES|TRIGGER)\b",
        )


class UserMCPRolloutPostgresPermissionsIntegrationTest(unittest.TestCase):
    def test_real_postgres_privileges_when_dedicated_dsn_is_configured(self) -> None:
        dsn = os.environ.get("MAF_POSTGRES_ROLLOUT_PERMISSIONS_TEST_DSN")
        if not dsn:
            self.skipTest("postgres_rollout_permissions_test_dsn_not_configured")

        try:
            import psycopg
        except (
            ImportError
        ) as exc:  # pragma: no cover - dependency is present in supported envs
            self.fail(f"psycopg is required for the PostgreSQL permission gate: {exc}")

        with psycopg.connect(dsn) as connection:
            try:
                with connection.cursor() as cursor:
                    cursor.execute(build_runtime_table_schema_ddl())
                    cursor.execute(PERMISSIONS_SQL)
                    cursor.execute(PERMISSIONS_SQL)
                    cursor.execute(
                        "SELECT rolname, rolcanlogin FROM pg_catalog.pg_roles WHERE rolname = ANY(%s)",
                        (list(ROLES),),
                    )
                    self.assertEqual(
                        dict(cursor.fetchall()), {role: False for role in ROLES}
                    )

                    for role in ROLES:
                        for table in ROLLOUT_TABLES:
                            cursor.execute(
                                "SELECT has_table_privilege(%s, %s, 'INSERT,UPDATE,DELETE,TRUNCATE')",
                                (role, f"public.{table}"),
                            )
                            self.assertFalse(
                                cursor.fetchone()[0], f"{role} has DML on {table}"
                            )

                    cursor.execute(
                        """
                        SELECT has_function_privilege(
                            'maf_rollout_ci_evidence_writer', p.oid, 'EXECUTE'
                        )
                        FROM pg_catalog.pg_proc AS p
                        JOIN pg_catalog.pg_namespace AS n ON n.oid = p.pronamespace
                        WHERE n.nspname = 'mcp_rollout_api'
                          AND p.proname = 'append_production_evidence_snapshot'
                        """
                    )
                    self.assertEqual(cursor.fetchone(), (False,))

                    cursor.execute(
                        """
                        SELECT c.relname
                        FROM pg_catalog.pg_trigger AS t
                        JOIN pg_catalog.pg_class AS c ON c.oid = t.tgrelid
                        JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
                        WHERE n.nspname = 'public'
                          AND t.tgname = 'mcp_rollout_history_append_only'
                          AND NOT t.tgisinternal
                        """
                    )
                    self.assertEqual(
                        {row[0] for row in cursor.fetchall()}, set(HISTORY_TABLES)
                    )
            finally:
                connection.rollback()


def _function_definition(function_name: str) -> str:
    match = re.search(
        rf"CREATE OR REPLACE FUNCTION mcp_rollout_api\.{re.escape(function_name)}\(.*?\$function\$;",
        PERMISSIONS_SQL,
        flags=re.DOTALL,
    )
    if match is None:
        raise AssertionError(f"function definition not found: {function_name}")
    return match.group(0)


def _function_grant(function_name: str) -> str:
    match = re.search(
        rf"GRANT EXECUTE ON FUNCTION mcp_rollout_api\.{re.escape(function_name)}\(.*?\)\s+TO\s+[^;]+;",
        PERMISSIONS_SQL,
        flags=re.DOTALL,
    )
    if match is None:
        raise AssertionError(f"function grant not found: {function_name}")
    return match.group(0)


def _sql_closed_values(function_definition: str, expression: str) -> frozenset[str]:
    match = re.search(
        rf"{re.escape(expression)}\s+NOT IN\s*\((.*?)\)",
        function_definition,
        flags=re.DOTALL,
    )
    if match is None:
        raise AssertionError(f"closed SQL values not found for: {expression}")
    return frozenset(re.findall(r"'([^']+)'", match.group(1)))


def _sql_matched_expectations(
    function_definition: str,
) -> dict[str, tuple[str, str, str, str]]:
    match = re.search(
        r"FROM \(VALUES(.*?)\) AS expected\(\s*scenario, legacy_outcome, "
        r"shadow_outcome,\s*transport, endpoint_policy\s*\)",
        function_definition,
        flags=re.DOTALL,
    )
    if match is None:
        raise AssertionError("matched scenario expectation matrix not found")
    rows = re.findall(
        r"\(\s*'([^']+)'\s*,\s*'([^']+)'\s*,\s*'([^']+)'\s*,"
        r"\s*'([^']+)'\s*,\s*'([^']+)'\s*\)",
        match.group(1),
        flags=re.DOTALL,
    )
    return {
        scenario: (legacy, shadow, transport, endpoint_policy)
        for scenario, legacy, shadow, transport, endpoint_policy in rows
    }
