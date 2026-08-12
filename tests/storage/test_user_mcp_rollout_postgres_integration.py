from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from threading import Barrier, Event
import time
import unittest
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from scripts.validate_user_mcp_phase3_evidence import parse_evidence_snapshot
from src.core.models import (
    MCPRolloutDeploymentActivation,
    MCPRolloutDrillObservation,
    MCPRolloutInstanceConfigLease,
    MCPRolloutMetricBucket,
    MCPRolloutBlockResolution,
    MCPRolloutPromotionBlock,
    MCPRolloutStageApproval,
    MCPShadowAuditSample,
    MCPRolloutGateScope,
    seal_mcp_rollout_drill_observation,
)
from src.integrations.mcp.observability import (
    mcp_evidence_snapshot_to_record,
    validate_mcp_evidence_snapshot_record,
)
from src.integrations.mcp.rollout_evidence import (
    MCPEvidenceKind,
    MCPEvidenceProducer,
    MCPEvidenceSnapshot,
    MCPEvidenceSource,
    MCPGateBlocker,
    MCPRolloutEvidencePayload,
    MCPRolloutStage,
    validate_evidence_snapshot,
)
from src.integrations.mcp.shadow_evidence import (
    MCP_SHADOW_SAMPLE_EXPECTATIONS,
    seal_shadow_audit_sample,
)
from src.state.postgres.runtime_schema import build_runtime_table_schema_ddl
from src.storage.postgres import (
    PostgreSQLStorage,
    create_postgres_engine,
    create_postgres_session_factory,
    validate_mcp_rollout_connection_role,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
PERMISSIONS_SQL = (
    REPO_ROOT / "scripts/postgres/user_mcp_rollout_permissions.sql"
).read_text(encoding="utf-8")
INTEGRATION_DSN_ENV = "MAF_POSTGRES_ROLLOUT_INTEGRATION_TEST_DSN"
ROLE_PASSWORD = f"phase3-{uuid4().hex}"
ROLE_GROUPS = {
    "app": "maf_rollout_app_writer",
    "snapshot": "maf_rollout_snapshot_producer",
    "ci": "maf_rollout_ci_evidence_writer",
    "evaluator": "maf_rollout_gate_evaluator",
    "operator": "maf_rollout_operator",
    "validator": "maf_rollout_validator",
    "drill": "maf_rollout_drill_recorder",
}
RED_LINES = (
    "cross_user_access",
    "secret_exposure",
    "dual_tool_call",
    "unauthorized_tool_call",
    "endpoint_policy_bypass",
    "unknown_result_replay",
    "shadow_tool_call",
    "persistent_resource_leak",
)
TERMINAL_RESULTS = ("succeeded", "failed", "unknown", "cancelled")


class UserMCPRolloutPostgresIntegrationTest(unittest.TestCase):
    """Real, independent-LOGIN PostgreSQL coverage for the D-4/D-5 boundary."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.admin_dsn = os.environ.get(INTEGRATION_DSN_ENV, "")
        if not cls.admin_dsn:
            raise unittest.SkipTest(
                "postgres_rollout_integration_test_dsn_not_configured"
            )
        try:
            import psycopg
            from psycopg.conninfo import conninfo_to_dict
        except ImportError as exc:  # pragma: no cover
            raise AssertionError("psycopg is required for the PostgreSQL gate") from exc
        cls.psycopg = psycopg
        cls._admin_conninfo = conninfo_to_dict(cls.admin_dsn)
        cls._admin_url = make_url(cls.admin_dsn)
        cls._login_suffix = uuid4().hex[:10]
        cls.login_names = {
            role: f"phase3_{role}_{cls._login_suffix}" for role in ROLE_GROUPS
        }
        with psycopg.connect(cls.admin_dsn, autocommit=True) as connection:
            connection.execute(build_runtime_table_schema_ddl())
            connection.execute(PERMISSIONS_SQL)
            for role, login in cls.login_names.items():
                connection.execute(
                    f"CREATE ROLE \"{login}\" LOGIN PASSWORD '{ROLE_PASSWORD}'"
                )
                connection.execute(
                    f'GRANT {ROLE_GROUPS[role]} TO "{login}" '
                    "WITH INHERIT TRUE, SET FALSE"
                )

    @classmethod
    def tearDownClass(cls) -> None:
        with cls.psycopg.connect(cls.admin_dsn, autocommit=True) as connection:
            for login in cls.login_names.values():
                connection.execute(f'DROP ROLE IF EXISTS "{login}"')

    def setUp(self) -> None:
        self.environment_id = f"rollout-integration-{uuid4().hex}"
        self.now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        self.engines = []

    def tearDown(self) -> None:
        with self.psycopg.connect(self.admin_dsn) as connection:
            connection.execute("SET LOCAL session_replication_role = replica")
            for table in (
                "mcp_rollout_block_resolution",
                "mcp_rollout_drill_observation",
                "mcp_rollout_instance_config",
                "mcp_rollout_deployment_activation",
                "mcp_rollout_promotion_block",
                "mcp_rollout_stage_approval",
                "mcp_rollout_evidence_snapshot",
                "mcp_rollout_metric_bucket",
                "mcp_shadow_audit_sample",
                "mcp_rollout_gate_scope",
            ):
                connection.execute(
                    f"DELETE FROM public.{table} WHERE environment_id = %s"
                    if table != "mcp_rollout_block_resolution"
                    else """
                    DELETE FROM public.mcp_rollout_block_resolution AS resolution
                    USING public.mcp_rollout_promotion_block AS block
                    WHERE resolution.block_id = block.block_id
                      AND block.environment_id = %s
                    """,
                    (self.environment_id,),
                )
            connection.commit()
        for engine in self.engines:
            engine.dispose()

    def test_independent_login_role_validation_and_base_dml_denial(self) -> None:
        for role in ROLE_GROUPS:
            with self.subTest(role=role):
                engine = self._engine(role)
                self.assertEqual(
                    validate_mcp_rollout_connection_role(engine, role),
                    self.login_names[role],
                )
                with self.assertRaises(self.psycopg.errors.InsufficientPrivilege):
                    with self._connection(role) as connection:
                        connection.execute(
                            """
                            INSERT INTO public.mcp_rollout_gate_scope
                                (environment_id, rollout_program, created_at)
                            VALUES (%s, 'user_mcp_phase3', %s)
                            """,
                            (self.environment_id, self.now),
                        )

        app_login = self.login_names["app"]
        with self.psycopg.connect(self.admin_dsn, autocommit=True) as connection:
            connection.execute(
                f"""
                GRANT EXECUTE ON FUNCTION mcp_rollout_api.append_stage_approval(
                    text, text, text, text, text, text, text, text, timestamptz
                ) TO "{app_login}"
                """
            )
        try:
            with self.assertRaisesRegex(RuntimeError, "unexpected function authority"):
                validate_mcp_rollout_connection_role(self._engine("app"), "app")
        finally:
            with self.psycopg.connect(self.admin_dsn, autocommit=True) as connection:
                connection.execute(
                    f"""
                    REVOKE EXECUTE ON FUNCTION mcp_rollout_api.append_stage_approval(
                        text, text, text, text, text, text, text, text, timestamptz
                    ) FROM "{app_login}"
                    """
                )
        with self.psycopg.connect(self.admin_dsn, autocommit=True) as connection:
            connection.execute(f'GRANT SELECT ON public.conversation TO "{app_login}"')
        try:
            with self.assertRaisesRegex(
                RuntimeError, "unexpected table read authority"
            ):
                validate_mcp_rollout_connection_role(self._engine("app"), "app")
        finally:
            with self.psycopg.connect(self.admin_dsn, autocommit=True) as connection:
                connection.execute(
                    f'REVOKE SELECT ON public.conversation FROM "{app_login}"'
                )

    def test_role_validation_rejects_set_role_identity_laundering(self) -> None:
        suffix = uuid4().hex[:12]
        intermediary = f"phase3_app_intermediary_{suffix}"
        combined_login = f"phase3_combined_login_{suffix}"
        combined_password = f"phase3-combined-{uuid4().hex}"
        engine = None
        with self.psycopg.connect(self.admin_dsn, autocommit=True) as connection:
            connection.execute(f'CREATE ROLE "{intermediary}" NOLOGIN')
            connection.execute(
                f'GRANT maf_rollout_app_writer TO "{intermediary}"'
            )
            connection.execute(
                f'CREATE ROLE "{combined_login}" LOGIN '
                f"PASSWORD '{combined_password}'"
            )
            connection.execute(
                f'GRANT "{intermediary}", maf_rollout_operator '
                f'TO "{combined_login}"'
            )
        try:
            laundering_dsn = self._admin_url.set(
                drivername="postgresql+psycopg",
                username=combined_login,
                password=combined_password,
            ).update_query_dict(
                {"options": f"-c role={intermediary}"}
            ).render_as_string(hide_password=False)
            engine = create_postgres_engine(
                laundering_dsn,
                pool_size=1,
                max_overflow=0,
            )
            with engine.connect() as connection:
                current_role, session_role = connection.execute(
                    text("SELECT CURRENT_USER, SESSION_USER")
                ).one()
            self.assertEqual(current_role, intermediary)
            self.assertEqual(session_role, combined_login)
            with self.assertRaisesRegex(RuntimeError, "over-privileged"):
                validate_mcp_rollout_connection_role(engine, "app")
        finally:
            if engine is not None:
                engine.dispose()
            with self.psycopg.connect(
                self.admin_dsn,
                autocommit=True,
            ) as connection:
                connection.execute(
                    f'REVOKE "{intermediary}", maf_rollout_operator '
                    f'FROM "{combined_login}"'
                )
                connection.execute(
                    f'REVOKE maf_rollout_app_writer FROM "{intermediary}"'
                )
                connection.execute(f'DROP ROLE "{combined_login}"')
                connection.execute(f'DROP ROLE "{intermediary}"')

    def test_role_validation_rejects_delegable_authority_membership(self) -> None:
        app_login = self.login_names["app"]
        sacrificial_role = f"phase3_delegated_{uuid4().hex[:12]}"
        with self.psycopg.connect(self.admin_dsn, autocommit=True) as connection:
            connection.execute(f'CREATE ROLE "{sacrificial_role}" NOLOGIN')
            connection.execute(
                f'GRANT maf_rollout_app_writer TO "{app_login}" '
                "WITH ADMIN TRUE, INHERIT TRUE, SET FALSE"
            )
        try:
            with self._connection("app") as connection:
                connection.execute(
                    f'GRANT maf_rollout_app_writer TO "{sacrificial_role}" '
                    "WITH INHERIT TRUE, SET FALSE"
                )
                connection.commit()
            with self.psycopg.connect(self.admin_dsn) as connection:
                delegated = connection.execute(
                    """
                    SELECT membership.admin_option,
                        membership.inherit_option, membership.set_option
                    FROM pg_catalog.pg_auth_members AS membership
                    JOIN pg_catalog.pg_roles AS granted_role
                      ON granted_role.oid = membership.roleid
                    JOIN pg_catalog.pg_roles AS member_role
                      ON member_role.oid = membership.member
                    WHERE granted_role.rolname = 'maf_rollout_app_writer'
                      AND member_role.rolname = %s
                    """,
                    (sacrificial_role,),
                ).fetchone()
            self.assertEqual(delegated, (False, True, False))
            with self.assertRaisesRegex(RuntimeError, "membership options"):
                validate_mcp_rollout_connection_role(
                    self._engine("app"),
                    "app",
                )
        finally:
            with self.psycopg.connect(
                self.admin_dsn,
                autocommit=True,
            ) as connection:
                connection.execute(
                    f'REVOKE maf_rollout_app_writer FROM "{app_login}" CASCADE'
                )
                connection.execute(
                    f'GRANT maf_rollout_app_writer TO "{app_login}" '
                    "WITH INHERIT TRUE, SET FALSE"
                )
                connection.execute(f'DROP ROLE "{sacrificial_role}"')

    def test_constrained_api_owner_preflight_rejects_owner_drift(self) -> None:
        engine = self._engine("app")
        self.assertEqual(
            validate_mcp_rollout_connection_role(engine, "app"),
            self.login_names["app"],
        )
        expected_select_and_insert = {
            "mcp_rollout_block_resolution",
            "mcp_rollout_deployment_activation",
            "mcp_rollout_drill_observation",
            "mcp_rollout_evidence_snapshot",
            "mcp_rollout_gate_scope",
            "mcp_rollout_instance_config",
            "mcp_rollout_metric_bucket",
            "mcp_rollout_promotion_block",
            "mcp_rollout_stage_approval",
            "mcp_shadow_audit_sample",
        }
        with self.psycopg.connect(self.admin_dsn) as connection:
            owner = connection.execute(
                """
                SELECT rolcanlogin, rolinherit, rolsuper, rolcreatedb,
                    rolcreaterole, rolreplication, rolbypassrls,
                    has_schema_privilege(
                        'maf_rollout_api_owner', 'mcp_rollout_api', 'CREATE'
                    ),
                    EXISTS (
                        SELECT 1 FROM pg_catalog.pg_auth_members AS membership
                        JOIN pg_catalog.pg_roles AS role
                          ON role.oid = membership.member
                          OR role.oid = membership.roleid
                        WHERE role.rolname = 'maf_rollout_api_owner'
                    )
                FROM pg_catalog.pg_roles
                WHERE rolname = 'maf_rollout_api_owner'
                """
            ).fetchone()
            self.assertEqual(owner, (False,) * 9)
            rows = connection.execute(
                """
                SELECT class.relname,
                    has_table_privilege(
                        'maf_rollout_api_owner', class.oid, 'SELECT'
                    ),
                    has_table_privilege(
                        'maf_rollout_api_owner', class.oid, 'INSERT'
                    ),
                    has_table_privilege(
                        'maf_rollout_api_owner', class.oid, 'UPDATE'
                    ),
                    has_table_privilege(
                        'maf_rollout_api_owner', class.oid, 'DELETE'
                    ),
                    has_table_privilege(
                        'maf_rollout_api_owner', class.oid,
                        'TRUNCATE,REFERENCES,TRIGGER'
                    )
                FROM pg_catalog.pg_class AS class
                JOIN pg_catalog.pg_namespace AS namespace
                  ON namespace.oid = class.relnamespace
                WHERE namespace.nspname = 'public'
                  AND class.relkind IN ('r', 'p', 'v', 'm', 'f')
                """
            ).fetchall()
        self.assertEqual({row[0] for row in rows if row[1]}, expected_select_and_insert)
        self.assertEqual({row[0] for row in rows if row[2]}, expected_select_and_insert)
        self.assertEqual(
            {row[0] for row in rows if row[3]},
            {
                "mcp_rollout_gate_scope",
                "mcp_rollout_instance_config",
                "mcp_rollout_metric_bucket",
            },
        )
        self.assertEqual(
            {row[0] for row in rows if row[4]}, {"mcp_shadow_audit_sample"}
        )
        self.assertFalse(any(row[5] for row in rows))

        with self.psycopg.connect(self.admin_dsn, autocommit=True) as connection:
            connection.execute("ALTER ROLE maf_rollout_api_owner LOGIN")
        try:
            with self.assertRaisesRegex(RuntimeError, "owner is not constrained"):
                validate_mcp_rollout_connection_role(engine, "app")
        finally:
            with self.psycopg.connect(self.admin_dsn, autocommit=True) as connection:
                connection.execute("ALTER ROLE maf_rollout_api_owner NOLOGIN")

        with self.psycopg.connect(self.admin_dsn, autocommit=True) as connection:
            connection.execute(
                """
                ALTER FUNCTION mcp_rollout_api.canonical_timestamp(timestamptz)
                OWNER TO CURRENT_USER
                """
            )
        try:
            with self.assertRaisesRegex(RuntimeError, "function ownership is invalid"):
                validate_mcp_rollout_connection_role(engine, "app")
        finally:
            with self.psycopg.connect(self.admin_dsn, autocommit=True) as connection:
                connection.execute(
                    """
                    ALTER FUNCTION mcp_rollout_api.canonical_timestamp(timestamptz)
                    OWNER TO maf_rollout_api_owner
                    """
                )

    def test_drill_login_appends_only_valid_drill_observations(self) -> None:
        observation = seal_mcp_rollout_drill_observation(
            MCPRolloutDrillObservation(
                drill_observation_id="drill-cancellation-passed",
                environment_id=self.environment_id,
                deployment_id="deploy-enforce",
                config_fingerprint="e" * 64,
                drill="cancellation",
                outcome="passed",
                observed_at=self.now,
                recorded_at=self.now,
                expires_at=self.now + timedelta(days=3),
                payload_digest="",
            )
        )
        saved = asyncio.run(
            self._storage("drill").append_mcp_rollout_drill_observation(observation)
        )
        self.assertEqual(saved, observation)
        with self.assertRaises(self.psycopg.errors.InsufficientPrivilege):
            with self._connection("app") as connection:
                self._execute_drill_direct(connection, observation)
        with self.assertRaises(self.psycopg.Error):
            with self._connection("drill") as connection:
                self._execute_drill_direct(
                    connection,
                    replace(
                        observation,
                        drill_observation_id="drill-tampered",
                        payload_digest="f" * 64,
                    ),
                )

    def test_app_definers_add_counter_set_gauge_and_recompute_sample_digest(
        self,
    ) -> None:
        storage = self._storage("app")
        bucket = self._bucket(value=2)
        first = asyncio.run(storage.upsert_mcp_rollout_metric_bucket(bucket))
        second = asyncio.run(storage.upsert_mcp_rollout_metric_bucket(bucket))
        self.assertEqual((first.value, second.value), (2, 4))
        absolute = asyncio.run(
            storage.set_mcp_rollout_metric_bucket(replace(bucket, value=7))
        )
        self.assertEqual(absolute.value, 7)

        sample = self._sample()
        self.assertEqual(
            asyncio.run(storage.save_mcp_shadow_audit_sample(sample)), sample
        )
        for index, (scenario, expectation) in enumerate(
            MCP_SHADOW_SAMPLE_EXPECTATIONS.items(),
            start=1,
        ):
            if scenario == sample.scenario:
                continue
            legacy_outcome, shadow_outcome, transport, endpoint_policy = expectation
            valid = seal_shadow_audit_sample(
                replace(
                    sample,
                    sample_id=f"sample-golden-{index}",
                    nonce=f"nonce-golden-{index}",
                    scenario=scenario,
                    legacy_outcome=legacy_outcome,
                    shadow_outcome=shadow_outcome,
                    transport=transport,
                    endpoint_policy=endpoint_policy,
                )
            )

            self.assertEqual(
                asyncio.run(storage.save_mcp_shadow_audit_sample(valid)),
                valid,
            )
        tampered = replace(sample, payload_digest="f" * 64)
        with self.assertRaises(self.psycopg.Error):
            self._append_sample_direct(tampered)

        hostile_changes = (
            ("owner-plaintext", {"safe_owner_ref": "owner-plain"}),
            ("task-uppercase", {"safe_task_ref": "hmac-sha256:" + "A" * 64}),
            ("call-short", {"safe_call_ref": "hmac-sha256:" + "3" * 63}),
            ("scenario", {"scenario": "future_scenario"}),
            ("legacy-outcome", {"legacy_outcome": "raw_success"}),
            ("shadow-outcome", {"shadow_outcome": "raw_success"}),
            ("transport", {"transport": "stdio"}),
            ("endpoint-policy", {"endpoint_policy": "caller_supplied"}),
            ("comparison", {"comparison": "close_enough"}),
            (
                "unknown-blocker",
                {"comparison": "mismatched", "blockers": ("future:blocker",)},
            ),
            (
                "matched-with-blocker",
                {
                    "comparison": "matched",
                    "blockers": ("shadow_outcome_mismatch",),
                },
            ),
            (
                "excluded-with-mismatch",
                {
                    "comparison": "excluded",
                    "blockers": ("shadow_outcome_mismatch",),
                },
            ),
            ("success-lane-swap", {"legacy_outcome": "control_plane_ready"}),
            ("auth-lane-swap", {"scenario": "authentication_failure"}),
            (
                "permission-lane-swap",
                {
                    "scenario": "permission_denial",
                    "shadow_outcome": "control_plane_ready",
                },
            ),
        )
        for label, changes in hostile_changes:
            with self.subTest(label=label):
                hostile = seal_shadow_audit_sample(
                    replace(
                        sample,
                        sample_id=f"sample-hostile-{label}",
                        nonce=f"nonce-hostile-{label}",
                        **changes,
                    )
                )
                with self.assertRaises(self.psycopg.Error):
                    self._append_sample_direct(hostile)
        with self.psycopg.connect(self.admin_dsn) as connection:
            saved_count = connection.execute(
                """
                SELECT count(*)
                FROM public.mcp_shadow_audit_sample
                WHERE environment_id = %s
                """,
                (self.environment_id,),
            ).fetchone()[0]
        self.assertEqual(saved_count, len(MCP_SHADOW_SAMPLE_EXPECTATIONS))

    def test_metric_writers_reject_nonminute_bucket_windows(self) -> None:
        bucket = self._bucket(value=1)
        hostile_buckets = (
            replace(
                bucket,
                metric_bucket_id="metric-coarse",
                bucket_ended_at=bucket.bucket_started_at + timedelta(minutes=2),
            ),
            replace(
                bucket,
                metric_bucket_id="metric-subminute",
                bucket_ended_at=bucket.bucket_started_at + timedelta(seconds=30),
            ),
            replace(
                bucket,
                metric_bucket_id="metric-unaligned",
                bucket_started_at=bucket.bucket_started_at + timedelta(seconds=1),
                bucket_ended_at=bucket.bucket_ended_at + timedelta(seconds=1),
            ),
        )
        storage = self._storage("app")
        for hostile in hostile_buckets:
            with self.subTest(repository=hostile.metric_bucket_id):
                with self.assertRaisesRegex(ValueError, "UTC-aligned minute"):
                    asyncio.run(storage.upsert_mcp_rollout_metric_bucket(hostile))

        for hostile in hostile_buckets:
            for additive in (True, False):
                with self.subTest(
                    definer=hostile.metric_bucket_id,
                    additive=additive,
                ):
                    with self.assertRaisesRegex(
                        self.psycopg.Error,
                        "one complete UTC-aligned minute",
                    ):
                        with self._connection("app") as connection:
                            self._execute_metric_direct(
                                connection,
                                hostile,
                                additive=additive,
                            )

    def test_shadow_sample_required_nulls_and_whitespace_fail_closed(self) -> None:
        sample = self._sample()
        accepted_null_refs = seal_shadow_audit_sample(
            replace(
                sample,
                sample_id="sample-null-safe-refs",
                nonce="nonce-null-safe-refs",
                safe_owner_ref=None,
                safe_task_ref=None,
                safe_call_ref=None,
            )
        )
        self._append_sample_direct(accepted_null_refs)

        required_null_fields = (
            "sample_id",
            "environment_id",
            "deployment_id",
            "config_fingerprint",
            "manifest_fingerprint",
            "fixture_fingerprint",
            "mapping_fingerprint",
            "scenario",
            "nonce",
            "legacy_outcome",
            "shadow_outcome",
            "transport",
            "endpoint_policy",
            "comparison",
            "blockers",
            "payload_digest",
            "observed_at",
            "recorded_at",
            "expires_at",
        )
        for index, field_name in enumerate(required_null_fields):
            with self.subTest(null_field=field_name):
                changes = {
                    "sample_id": f"sample-null-{index}",
                    "nonce": f"nonce-null-{index}",
                    field_name: None,
                }
                candidate = replace(sample, **changes)
                if field_name != "payload_digest":
                    candidate = seal_shadow_audit_sample(candidate)
                with self.assertRaises(self.psycopg.Error):
                    self._append_sample_direct(candidate)

        for index, field_name in enumerate(
            (
                "sample_id",
                "environment_id",
                "deployment_id",
                "scenario",
                "nonce",
                "legacy_outcome",
                "shadow_outcome",
                "transport",
                "endpoint_policy",
                "comparison",
            )
        ):
            with self.subTest(whitespace_field=field_name):
                changes = {
                    "sample_id": f"sample-space-{index}",
                    "nonce": f"nonce-space-{index}",
                    field_name: "   ",
                }
                candidate = seal_shadow_audit_sample(replace(sample, **changes))
                with self.assertRaises(self.psycopg.Error):
                    self._append_sample_direct(candidate)

    def test_caller_authored_production_json_is_unexecutable_for_every_runtime_role(
        self,
    ) -> None:
        statement = """
            SELECT mcp_rollout_api.append_production_evidence_snapshot(
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s::jsonb, %s, %s, %s
            )
        """
        arguments = (
            "evidence",
            self.environment_id,
            "a" * 40,
            "deployment",
            "internal_shadow",
            "b" * 64,
            self.now,
            self.now + timedelta(minutes=1),
            self.now + timedelta(minutes=1),
            1,
            "nonce",
            "internal_shadow",
            '{"kind":"internal_shadow"}',
            "c" * 64,
            "key",
            "d" * 64,
        )
        for role in ROLE_GROUPS:
            with self.subTest(role=role):
                with self.assertRaises(self.psycopg.errors.InsufficientPrivilege):
                    with self._connection(role) as connection:
                        connection.execute(statement, arguments)

    def test_prepare_sign_finalize_uses_db_payload_and_python_golden_digest(
        self,
    ) -> None:
        self._insert_active_stage(
            deployment_id=self._deployment_id,
            stage="internal_shadow",
            config_fingerprint="a" * 64,
        )
        app = self._storage("app")
        asyncio.run(app.save_mcp_shadow_audit_sample(self._sample()))
        asyncio.run(app.upsert_mcp_rollout_metric_bucket(self._bucket(value=0)))
        key = b"phase3-integration-attestation-key"
        snapshot = asyncio.run(
            self._storage("snapshot").produce_mcp_shadow_evidence_snapshot_db_derived(
                self.environment_id,
                self._deployment_id,
                git_sha="a" * 40,
                window_started_at=self.now - timedelta(minutes=1),
                window_ended_at=self.now + timedelta(minutes=1),
                attestation_key_id="integration-key",
                attestation_key=key,
            )
        )
        self.assertEqual(snapshot.source, "production")
        self.assertEqual(snapshot.payload["manifest_fingerprint"], "b" * 64)
        self.assertEqual(
            validate_mcp_evidence_snapshot_record(
                snapshot, trusted_attestation_keys={"integration-key": key}
            ),
            (),
        )

    def test_cross_transaction_materialization_drift_fails_closed(self) -> None:
        self._insert_active_stage(
            deployment_id=self._deployment_id,
            stage="internal_shadow",
            config_fingerprint="a" * 64,
        )
        app = self._storage("app")
        asyncio.run(app.save_mcp_shadow_audit_sample(self._sample()))
        parameters = (
            self.environment_id,
            self._deployment_id,
            "a" * 40,
            self.now - timedelta(minutes=1),
            self.now + timedelta(minutes=1),
        )
        with self._connection("snapshot") as connection:
            prepared = connection.execute(
                """
                SELECT * FROM mcp_rollout_api.prepare_production_evidence_snapshot(
                    %s, %s, %s, %s, %s
                )
                """,
                parameters,
            ).fetchone()
            connection.commit()
        asyncio.run(
            app.save_mcp_shadow_audit_sample(
                self._sample(sample_id="sample-2", nonce="nonce-2")
            )
        )
        with self.assertRaisesRegex(self.psycopg.Error, "materialization changed"):
            with self._connection("snapshot") as connection:
                connection.execute(
                    """
                    SELECT mcp_rollout_api.finalize_production_evidence_snapshot(
                        %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    """,
                    (*parameters, prepared[0], prepared[16], "key", "e" * 64),
                )

    def test_db_derived_materializer_covers_every_enforcement_stage(self) -> None:
        window_started_at = self.now - timedelta(minutes=2)
        window_ended_at = self.now + timedelta(minutes=2)
        for stage in (
            "internal_enforce",
            "cohort_enforce",
            "full_enforce",
            "legacy_assembly_off",
        ):
            with self.subTest(stage=stage):
                deployment_id = f"deploy-{stage}"
                config_fingerprint = {
                    "internal_enforce": "1",
                    "cohort_enforce": "2",
                    "full_enforce": "3",
                    "legacy_assembly_off": "4",
                }[stage] * 64
                self._insert_active_stage(
                    deployment_id=deployment_id,
                    stage=stage,
                    config_fingerprint=config_fingerprint,
                )
                call_kinds = (
                    ("ordinary",)
                    if stage == "internal_enforce"
                    else ("ordinary", "remote_task")
                )
                self._seed_required_metrics(
                    deployment_id=deployment_id,
                    stage=stage,
                    config_fingerprint=config_fingerprint,
                    window_started_at=window_started_at,
                    window_ended_at=window_ended_at,
                    call_kinds=call_kinds,
                )
                if stage == "internal_enforce":
                    self._seed_passed_drills(
                        deployment_id=deployment_id,
                        config_fingerprint=config_fingerprint,
                    )
                snapshot = asyncio.run(
                    self._storage(
                        "snapshot"
                    ).produce_mcp_rollout_evidence_snapshot_db_derived(
                        self.environment_id,
                        deployment_id,
                        git_sha="a" * 40,
                        window_started_at=window_started_at,
                        window_ended_at=window_ended_at,
                        attestation_key_id="integration-key",
                        attestation_key=b"phase3-integration-attestation-key",
                    )
                )
                self.assertEqual(snapshot.stage, stage)
                self.assertEqual(snapshot.config_fingerprint, config_fingerprint)
                self.assertEqual(snapshot.payload["kind"], stage)
                self.assertTrue(snapshot.payload["continuous_window"])
                self.assertEqual(snapshot.payload["missing_bucket_count"], 0)
                self.assertEqual(
                    {item["call_kind"] for item in snapshot.payload["call_kinds"]},
                    set(call_kinds),
                )
                self.assertEqual(
                    set(snapshot.payload["completed_drills"]),
                    set(self._drill_names()) if stage == "internal_enforce" else set(),
                )

    def test_required_series_completeness_uses_exact_minute_buckets(self) -> None:
        window_started_at = self.now - timedelta(minutes=2)
        window_ended_at = self.now + timedelta(minutes=2)
        minute_intervals = tuple(
            (started_at, started_at + timedelta(minutes=1))
            for started_at in (
                window_started_at + timedelta(minutes=offset)
                for offset in range(4)
            )
        )
        cases = {
            "optional_gap_is_ignored": {
                "target_intervals": minute_intervals,
                "omit_target": False,
                "optional_intervals": (
                    minute_intervals[0],
                    minute_intervals[-1],
                ),
                "missing": 0,
            },
            "one_required_redline_missing": {
                "target_intervals": (),
                "omit_target": True,
                "optional_intervals": (),
                "missing": 1,
            },
            "required_internal_gap": {
                "target_intervals": minute_intervals[:1] + minute_intervals[2:],
                "omit_target": False,
                "optional_intervals": (),
                "missing": 1,
            },
            "required_starts_late": {
                "target_intervals": minute_intervals[1:],
                "omit_target": False,
                "optional_intervals": (),
                "missing": 1,
            },
            "required_ends_early": {
                "target_intervals": minute_intervals[:-1],
                "omit_target": False,
                "optional_intervals": (),
                "missing": 1,
            },
        }
        for index, (label, case) in enumerate(cases.items(), start=1):
            with self.subTest(case=label):
                deployment_id = f"deploy-completeness-{index}"
                config_fingerprint = f"{index:x}" * 64
                self._insert_active_stage(
                    deployment_id=deployment_id,
                    stage="internal_enforce",
                    config_fingerprint=config_fingerprint,
                )
                intervals = {
                    red_line: minute_intervals
                    for red_line in RED_LINES
                    if red_line != "persistent_resource_leak"
                }
                if not case["omit_target"]:
                    intervals["persistent_resource_leak"] = case["target_intervals"]
                self._seed_required_metrics(
                    deployment_id=deployment_id,
                    stage="internal_enforce",
                    config_fingerprint=config_fingerprint,
                    window_started_at=window_started_at,
                    window_ended_at=window_ended_at,
                    call_kinds=("ordinary",),
                    red_line_intervals=intervals,
                )
                for optional_started_at, optional_ended_at in case[
                    "optional_intervals"
                ]:
                    self._save_metric(
                        replace(
                            self._bucket(value=0),
                            metric_bucket_id=f"optional-{label}-{uuid4().hex}",
                            deployment_id=deployment_id,
                            stage="internal_enforce",
                            config_fingerprint=config_fingerprint,
                            bucket_started_at=optional_started_at,
                            bucket_ended_at=optional_ended_at,
                            created_at=optional_started_at,
                            updated_at=optional_ended_at,
                        )
                    )
                payload = self._prepare_payload(
                    deployment_id=deployment_id,
                    window_started_at=window_started_at,
                    window_ended_at=window_ended_at,
                )
                self.assertEqual(payload["missing_bucket_count"], case["missing"])
                self.assertEqual(payload["continuous_window"], case["missing"] == 0)
                if label in {"optional_gap_is_ignored", "required_internal_gap"}:
                    key = b"phase3-integration-attestation-key"
                    snapshot = asyncio.run(
                        self._storage(
                            "snapshot"
                        ).produce_mcp_rollout_evidence_snapshot_db_derived(
                            self.environment_id,
                            deployment_id,
                            git_sha="a" * 40,
                            window_started_at=window_started_at,
                            window_ended_at=window_ended_at,
                            attestation_key_id="integration-key",
                            attestation_key=key,
                        )
                    )
                    blockers = validate_mcp_evidence_snapshot_record(
                        snapshot,
                        trusted_attestation_keys={"integration-key": key},
                    )
                    self.assertEqual(blockers, ())
                    typed_snapshot = parse_evidence_snapshot(
                        {
                            field: (
                                getattr(snapshot, field).isoformat()
                                if field
                                in {
                                    "window_started_at",
                                    "window_ended_at",
                                    "recorded_at",
                                }
                                else getattr(snapshot, field)
                            )
                            for field in (
                                "evidence_id",
                                "environment_id",
                                "git_sha",
                                "deployment_id",
                                "stage",
                                "config_fingerprint",
                                "window_started_at",
                                "window_ended_at",
                                "recorded_at",
                                "producer",
                                "source",
                                "snapshot_id",
                                "nonce",
                                "payload",
                                "payload_digest",
                                "attestation_key_id",
                                "attestation_signature",
                            )
                        }
                    )
                    blockers = validate_evidence_snapshot(
                        typed_snapshot,
                        trusted_attestation_keys={"integration-key": key},
                    )
                    if label == "optional_gap_is_ignored":
                        self.assertNotIn(MCPGateBlocker.WINDOW_INCOMPLETE, blockers)
                    else:
                        self.assertIn(MCPGateBlocker.WINDOW_INCOMPLETE, blockers)

    def test_operator_owns_approval_and_db_rejects_wrong_transition(self) -> None:
        evidence = self._ci_evidence()
        ci = self._storage("ci")
        record = mcp_evidence_snapshot_to_record(evidence)
        asyncio.run(ci.append_mcp_rollout_evidence_snapshot(record))
        approval = MCPRolloutStageApproval(
            approval_id="approval-good",
            environment_id=self.environment_id,
            deployment_id="target-shadow",
            stage="internal_shadow",
            config_fingerprint="d" * 64,
            evidence_id=evidence.evidence_id,
            reason="approved CI conformance",
            approver="operator",
            created_at=self.now + timedelta(minutes=2),
        )
        saved = asyncio.run(
            self._storage("operator").append_mcp_rollout_stage_approval(approval)
        )
        self.assertEqual(saved.approval_id, approval.approval_id)
        with self.assertRaises(self.psycopg.errors.InsufficientPrivilege):
            with self._connection("evaluator") as connection:
                connection.execute(
                    """
                    SELECT mcp_rollout_api.append_stage_approval(
                        %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    """,
                    (
                        "evaluator-forgery",
                        self.environment_id,
                        "target",
                        "internal_shadow",
                        "f" * 64,
                        evidence.evidence_id,
                        "forgery",
                        "evaluator",
                        self.now + timedelta(minutes=2),
                    ),
                )
        with self.assertRaisesRegex(DBAPIError, "transition"):
            asyncio.run(
                self._storage("operator").append_mcp_rollout_stage_approval(
                    replace(
                        approval,
                        approval_id="approval-bad",
                        deployment_id="target-enforce",
                        stage="internal_enforce",
                        config_fingerprint="e" * 64,
                    )
                )
            )

    def test_positive_red_line_metric_atomically_creates_one_derived_block(
        self,
    ) -> None:
        evidence = self._ci_evidence()
        asyncio.run(
            self._storage("ci").append_mcp_rollout_evidence_snapshot(
                mcp_evidence_snapshot_to_record(evidence)
            )
        )
        operator = self._storage("operator")
        approval = asyncio.run(
            operator.append_mcp_rollout_stage_approval(
                MCPRolloutStageApproval(
                    approval_id="approval-redline",
                    environment_id=self.environment_id,
                    deployment_id=self._deployment_id,
                    stage="internal_shadow",
                    config_fingerprint="a" * 64,
                    evidence_id=evidence.evidence_id,
                    reason="activate shadow",
                    approver="operator",
                    created_at=self.now + timedelta(minutes=2),
                )
            )
        )
        asyncio.run(
            operator.activate_mcp_rollout_deployment(
                MCPRolloutDeploymentActivation(
                    activation_id="activation-redline",
                    environment_id=self.environment_id,
                    deployment_id=self._deployment_id,
                    stage="internal_shadow",
                    config_fingerprint="a" * 64,
                    approval_id=approval.approval_id,
                    evidence_id=evidence.evidence_id,
                    previous_activation_id=None,
                    operator_reason="activate shadow",
                    is_rollback=False,
                    created_at=self.now + timedelta(minutes=3),
                )
            )
        )
        app = self._storage("app")
        red_line_template = replace(
            self._bucket(value=1),
            metric_name="mcp_safety_red_line_total",
            red_line="cross_user_access",
            result_category="failed",
            error_category="validation",
        )
        for value in (1, 0, -1):
            with self.subTest(setter_value=value):
                with self.assertRaisesRegex(DBAPIError, "additive-counter-only"):
                    asyncio.run(
                        app.set_mcp_rollout_metric_bucket(
                            replace(
                                red_line_template,
                                metric_bucket_id=f"metric-set-redline-{value}",
                                value=value,
                            )
                        )
                    )
        with self.psycopg.connect(self.admin_dsn) as connection:
            metric_count = connection.execute(
                """
                SELECT count(*)
                FROM public.mcp_rollout_metric_bucket
                WHERE environment_id = %s
                """,
                (self.environment_id,),
            ).fetchone()[0]
            block_count = connection.execute(
                """
                SELECT count(*)
                FROM public.mcp_rollout_promotion_block
                WHERE environment_id = %s
                """,
                (self.environment_id,),
            ).fetchone()[0]
        self.assertEqual((metric_count, block_count), (0, 0))

        zero = asyncio.run(
            app.upsert_mcp_rollout_metric_bucket(
                replace(
                    red_line_template,
                    metric_bucket_id="metric-add-redline-zero",
                    value=0,
                )
            )
        )
        self.assertEqual(zero.value, 0)
        with self.assertRaises(DBAPIError):
            asyncio.run(
                app.upsert_mcp_rollout_metric_bucket(
                    replace(
                        red_line_template,
                        metric_bucket_id="metric-add-redline-negative",
                        value=-1,
                    )
                )
            )
        with self.psycopg.connect(self.admin_dsn) as connection:
            block_count = connection.execute(
                """
                SELECT count(*)
                FROM public.mcp_rollout_promotion_block
                WHERE environment_id = %s
                """,
                (self.environment_id,),
            ).fetchone()[0]
        self.assertEqual(block_count, 0)

        for red_line in ("secret_exposure", "dual_tool_call"):
            asyncio.run(
                app.upsert_mcp_rollout_metric_bucket(
                    replace(
                        self._bucket(value=1),
                        metric_bucket_id=f"metric-{red_line}",
                        metric_name="mcp_safety_red_line_total",
                        red_line=red_line,
                        result_category="failed",
                        error_category="validation",
                    )
                )
            )
        with self.assertRaisesRegex(DBAPIError, "non-negative"):
            asyncio.run(
                app.upsert_mcp_rollout_metric_bucket(
                    replace(
                        red_line_template,
                        metric_bucket_id="metric-secret-exposure-negative-replay",
                        red_line="secret_exposure",
                        value=-1,
                    )
                )
            )
        with self.psycopg.connect(self.admin_dsn) as connection:
            rows = connection.execute(
                """
                SELECT reason_code, evidence_id
                FROM public.mcp_rollout_promotion_block
                WHERE environment_id = %s
                """,
                (self.environment_id,),
            ).fetchall()
            secret_exposure_value = connection.execute(
                """
                SELECT value
                FROM public.mcp_rollout_metric_bucket
                WHERE environment_id = %s
                  AND metric_name = 'mcp_safety_red_line_total'
                  AND red_line = 'secret_exposure'
                """,
                (self.environment_id,),
            ).fetchone()[0]
        self.assertEqual(rows, [("safety_red_line_nonzero", evidence.evidence_id)])
        self.assertEqual(secret_exposure_value, 1)

    def test_normal_writers_skip_gate_lock_and_missing_activation_redline_rolls_back(
        self,
    ) -> None:
        operator = self._storage("operator")
        asyncio.run(
            operator.ensure_mcp_rollout_gate_scope(
                self._gate_scope(created_at=self.now)
            )
        )
        with self.psycopg.connect(self.admin_dsn) as lock_connection:
            lock_connection.execute(
                """
                SELECT 1
                FROM public.mcp_rollout_gate_scope
                WHERE environment_id = %s
                  AND rollout_program = 'user_mcp_phase3'
                FOR UPDATE
                """,
                (self.environment_id,),
            )
            with self._connection("app") as app_connection:
                app_connection.execute("SET LOCAL lock_timeout = '250ms'")
                metric = self._execute_metric_direct(
                    app_connection, self._bucket(value=1)
                )
                gauge = self._execute_metric_direct(
                    app_connection,
                    replace(
                        self._bucket(value=7),
                        metric_bucket_id="metric-gauge",
                        metric_name="mcp_gateway_active_scopes",
                    ),
                    additive=False,
                )
                sample = self._execute_sample_direct(app_connection, self._sample())
                app_connection.commit()
            self.assertEqual(metric["value"], 1)
            self.assertEqual(gauge["value"], 7)
            self.assertEqual(sample["sample_id"], "sample-1")

        red_line = replace(
            self._bucket(value=1),
            metric_bucket_id="metric-redline-missing-activation",
            metric_name="mcp_safety_red_line_total",
            red_line="secret_exposure",
            result_category="failed",
            error_category="validation",
        )
        with self.assertRaisesRegex(DBAPIError, "no exact activation"):
            asyncio.run(self._storage("app").upsert_mcp_rollout_metric_bucket(red_line))
        with self.psycopg.connect(self.admin_dsn) as connection:
            metric_count = connection.execute(
                """
                SELECT count(*)
                FROM public.mcp_rollout_metric_bucket
                WHERE environment_id = %s
                  AND metric_name = 'mcp_safety_red_line_total'
                """,
                (self.environment_id,),
            ).fetchone()[0]
            block_count = connection.execute(
                """
                SELECT count(*)
                FROM public.mcp_rollout_promotion_block
                WHERE environment_id = %s
                """,
                (self.environment_id,),
            ).fetchone()[0]
        self.assertEqual((metric_count, block_count), (0, 0))

    def test_nonce_snapshot_and_approval_activation_concurrency_fail_closed(
        self,
    ) -> None:
        first = self._ci_evidence(evidence_id="ci-race-a", nonce="nonce-race")
        second = self._ci_evidence(evidence_id="ci-race-b", nonce="nonce-race")
        ci_storages = (self._storage("ci"), self._storage("ci"))
        barrier = Barrier(2)

        def append_evidence(storage, snapshot):
            barrier.wait()
            try:
                asyncio.run(
                    storage.append_mcp_rollout_evidence_snapshot(
                        mcp_evidence_snapshot_to_record(snapshot)
                    )
                )
            except DBAPIError:
                return False
            return True

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(append_evidence, ci_storages, (first, second)))
        self.assertEqual(sorted(results), [False, True])
        with self.psycopg.connect(self.admin_dsn) as connection:
            winning_evidence_id = connection.execute(
                """
                SELECT evidence_id
                FROM public.mcp_rollout_evidence_snapshot
                WHERE environment_id = %s
                """,
                (self.environment_id,),
            ).fetchone()[0]

        approval = asyncio.run(
            self._storage("operator").append_mcp_rollout_stage_approval(
                MCPRolloutStageApproval(
                    approval_id="approval-race",
                    environment_id=self.environment_id,
                    deployment_id="deploy-race-shadow",
                    stage="internal_shadow",
                    config_fingerprint="9" * 64,
                    evidence_id=winning_evidence_id,
                    reason="consume approval once",
                    approver="operator",
                    created_at=self.now + timedelta(minutes=2),
                )
            )
        )
        activations = (
            MCPRolloutDeploymentActivation(
                activation_id="activation-race-a",
                environment_id=self.environment_id,
                deployment_id=approval.deployment_id,
                stage=approval.stage,
                config_fingerprint=approval.config_fingerprint,
                approval_id=approval.approval_id,
                evidence_id=approval.evidence_id,
                previous_activation_id=None,
                operator_reason="atomic approval consumption",
                is_rollback=False,
                created_at=self.now + timedelta(minutes=3),
            ),
            MCPRolloutDeploymentActivation(
                activation_id="activation-race-b",
                environment_id=self.environment_id,
                deployment_id=approval.deployment_id,
                stage=approval.stage,
                config_fingerprint=approval.config_fingerprint,
                approval_id=approval.approval_id,
                evidence_id=approval.evidence_id,
                previous_activation_id=None,
                operator_reason="atomic approval consumption",
                is_rollback=False,
                created_at=self.now + timedelta(minutes=3),
            ),
        )
        operator_storages = (self._storage("operator"), self._storage("operator"))
        barrier = Barrier(2)

        def activate(storage, activation):
            barrier.wait()
            try:
                asyncio.run(storage.activate_mcp_rollout_deployment(activation))
            except DBAPIError:
                return False
            return True

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(activate, operator_storages, activations))
        self.assertEqual(sorted(results), [False, True])
        with self.psycopg.connect(self.admin_dsn) as connection:
            count = connection.execute(
                """
                SELECT count(*)
                FROM public.mcp_rollout_deployment_activation
                WHERE environment_id = %s
                """,
                (self.environment_id,),
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_cross_stage_block_serializes_before_activation(self) -> None:
        evidence = self._ci_evidence(evidence_id="ci-cross-stage")
        asyncio.run(
            self._storage("ci").append_mcp_rollout_evidence_snapshot(
                mcp_evidence_snapshot_to_record(evidence)
            )
        )
        approval = asyncio.run(
            self._storage("operator").append_mcp_rollout_stage_approval(
                MCPRolloutStageApproval(
                    approval_id="approval-cross-stage",
                    environment_id=self.environment_id,
                    deployment_id="deploy-cross-stage",
                    stage="internal_shadow",
                    config_fingerprint="8" * 64,
                    evidence_id=evidence.evidence_id,
                    reason="approved before concurrent block",
                    approver="operator",
                    created_at=self.now + timedelta(minutes=2),
                )
            )
        )
        block = MCPRolloutPromotionBlock(
            block_id="block-cross-stage",
            environment_id=self.environment_id,
            deployment_id=evidence.deployment_id,
            stage=evidence.stage.value,
            config_fingerprint=evidence.config_fingerprint,
            evidence_id=evidence.evidence_id,
            reason_code="window_incomplete",
            created_at=self.now + timedelta(minutes=3),
        )
        application_name = f"phase3-cross-stage-{self.environment_id[-12:]}"
        worker_started = Event()

        def activate_while_locked() -> str:
            with self._connection("operator") as connection:
                connection.execute(
                    "SELECT pg_catalog.set_config('application_name', %s, false)",
                    (application_name,),
                )
                connection.execute("SET LOCAL lock_timeout = '3s'")
                worker_started.set()
                try:
                    self._execute_activation_direct(
                        connection,
                        MCPRolloutDeploymentActivation(
                            activation_id="activation-cross-stage",
                            environment_id=self.environment_id,
                            deployment_id=approval.deployment_id,
                            stage=approval.stage,
                            config_fingerprint=approval.config_fingerprint,
                            approval_id=approval.approval_id,
                            evidence_id=approval.evidence_id,
                            previous_activation_id=None,
                            operator_reason="must observe cross-stage block",
                            is_rollback=False,
                            created_at=self.now + timedelta(minutes=4),
                        ),
                    )
                    connection.commit()
                except self.psycopg.Error as exc:
                    connection.rollback()
                    return str(exc)
            return "activated"

        with self._connection("evaluator") as blocking_connection:
            self._execute_block_direct(blocking_connection, block)
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(activate_while_locked)
                self.assertTrue(worker_started.wait(timeout=3))
                saw_lock_wait = self._wait_for_lock(application_name)
                blocking_connection.commit()
                outcome = future.result(timeout=4)
        self.assertTrue(saw_lock_wait)
        self.assertIn("active promotion block", outcome)
        with self.psycopg.connect(self.admin_dsn) as connection:
            activation_count = connection.execute(
                """
                SELECT count(*)
                FROM public.mcp_rollout_deployment_activation
                WHERE environment_id = %s
                """,
                (self.environment_id,),
            ).fetchone()[0]
        self.assertEqual(activation_count, 0)

    def test_active_block_allows_only_strict_rollback_and_exact_instance_leases(
        self,
    ) -> None:
        operator = self._storage("operator")
        shadow_evidence = self._admin_production_evidence(
            evidence_id="prod-shadow-current",
            deployment_id="deploy-shadow-current",
            stage=MCPRolloutStage.INTERNAL_SHADOW,
            config_fingerprint="1" * 64,
            snapshot_id=1,
            offset_seconds=0,
        )
        enforce_approval = asyncio.run(
            operator.append_mcp_rollout_stage_approval(
                MCPRolloutStageApproval(
                    approval_id="approval-enforce-current",
                    environment_id=self.environment_id,
                    deployment_id="deploy-enforce-current",
                    stage="internal_enforce",
                    config_fingerprint="2" * 64,
                    evidence_id=shadow_evidence.evidence_id,
                    reason="establish current enforce activation",
                    approver="operator",
                    created_at=self.now + timedelta(seconds=1),
                )
            )
        )
        current_activation = asyncio.run(
            operator.activate_mcp_rollout_deployment(
                MCPRolloutDeploymentActivation(
                    activation_id="activation-enforce-current",
                    environment_id=self.environment_id,
                    deployment_id=enforce_approval.deployment_id,
                    stage=enforce_approval.stage,
                    config_fingerprint=enforce_approval.config_fingerprint,
                    approval_id=enforce_approval.approval_id,
                    evidence_id=enforce_approval.evidence_id,
                    previous_activation_id=None,
                    operator_reason="establish enforce",
                    is_rollback=False,
                    created_at=self.now + timedelta(seconds=2),
                )
            )
        )
        enforce_evidence = self._admin_production_evidence(
            evidence_id="prod-enforce-current",
            deployment_id=current_activation.deployment_id,
            stage=MCPRolloutStage.INTERNAL_ENFORCE,
            config_fingerprint=current_activation.config_fingerprint,
            snapshot_id=1,
            offset_seconds=10,
        )
        asyncio.run(
            self._storage("evaluator").append_mcp_rollout_promotion_block(
                MCPRolloutPromotionBlock(
                    block_id="block-enforce-current",
                    environment_id=self.environment_id,
                    deployment_id=enforce_evidence.deployment_id,
                    stage=enforce_evidence.stage.value,
                    config_fingerprint=enforce_evidence.config_fingerprint,
                    evidence_id=enforce_evidence.evidence_id,
                    reason_code="safety_red_line",
                    created_at=self.now + timedelta(seconds=11),
                )
            )
        )
        rollback_approval = asyncio.run(
            operator.append_mcp_rollout_stage_approval(
                MCPRolloutStageApproval(
                    approval_id="approval-strict-rollback",
                    environment_id=self.environment_id,
                    deployment_id="deploy-shadow-rollback",
                    stage="internal_shadow",
                    config_fingerprint="3" * 64,
                    evidence_id=enforce_evidence.evidence_id,
                    reason="strictly reduce exposure",
                    approver="operator",
                    created_at=self.now + timedelta(seconds=12),
                )
            )
        )
        rollback = asyncio.run(
            operator.activate_mcp_rollout_deployment(
                MCPRolloutDeploymentActivation(
                    activation_id="activation-strict-rollback",
                    environment_id=self.environment_id,
                    deployment_id=rollback_approval.deployment_id,
                    stage=rollback_approval.stage,
                    config_fingerprint=rollback_approval.config_fingerprint,
                    approval_id=rollback_approval.approval_id,
                    evidence_id=rollback_approval.evidence_id,
                    previous_activation_id=current_activation.activation_id,
                    operator_reason="active block requires strict rollback",
                    is_rollback=True,
                    created_at=self.now + timedelta(seconds=13),
                )
            )
        )
        app = self._storage("app")
        for instance_id in ("api-a", "api-b"):
            asyncio.run(
                app.save_mcp_rollout_instance_config_lease(
                    MCPRolloutInstanceConfigLease(
                        instance_config_id=f"lease-{instance_id}",
                        environment_id=self.environment_id,
                        deployment_id=rollback.deployment_id,
                        instance_id=instance_id,
                        stage=rollback.stage,
                        config_fingerprint=rollback.config_fingerprint,
                        activation_id=rollback.activation_id,
                        lease_expires_at=self.now + timedelta(hours=1),
                        created_at=self.now + timedelta(seconds=14),
                        updated_at=self.now + timedelta(seconds=14),
                    )
                )
            )
        bad_lease = MCPRolloutInstanceConfigLease(
            instance_config_id="lease-api-c",
            environment_id=self.environment_id,
            deployment_id=rollback.deployment_id,
            instance_id="api-c",
            stage=rollback.stage,
            config_fingerprint="4" * 64,
            activation_id=rollback.activation_id,
            lease_expires_at=self.now + timedelta(hours=1),
            created_at=self.now + timedelta(seconds=15),
            updated_at=self.now + timedelta(seconds=15),
        )
        with self.assertRaisesRegex(DBAPIError, "config fingerprint mismatch"):
            asyncio.run(app.save_mcp_rollout_instance_config_lease(bad_lease))

        same_stage_evidence = self._admin_production_evidence(
            evidence_id="prod-shadow-same-stage",
            deployment_id=rollback.deployment_id,
            stage=MCPRolloutStage.INTERNAL_SHADOW,
            config_fingerprint=rollback.config_fingerprint,
            snapshot_id=1,
            offset_seconds=20,
        )
        same_stage_approval = asyncio.run(
            operator.append_mcp_rollout_stage_approval(
                MCPRolloutStageApproval(
                    approval_id="approval-not-strict",
                    environment_id=self.environment_id,
                    deployment_id="deploy-shadow-same-stage",
                    stage="internal_shadow",
                    config_fingerprint="5" * 64,
                    evidence_id=same_stage_evidence.evidence_id,
                    reason="attempt same exposure rollback",
                    approver="operator",
                    created_at=self.now + timedelta(seconds=21),
                )
            )
        )
        with self.assertRaisesRegex(DBAPIError, "rollback evidence or target"):
            asyncio.run(
                operator.activate_mcp_rollout_deployment(
                    MCPRolloutDeploymentActivation(
                        activation_id="activation-not-strict",
                        environment_id=self.environment_id,
                        deployment_id=same_stage_approval.deployment_id,
                        stage=same_stage_approval.stage,
                        config_fingerprint=same_stage_approval.config_fingerprint,
                        approval_id=same_stage_approval.approval_id,
                        evidence_id=same_stage_approval.evidence_id,
                        previous_activation_id=rollback.activation_id,
                        operator_reason="must fail same exposure rollback",
                        is_rollback=True,
                        created_at=self.now + timedelta(seconds=22),
                    )
                )
            )
        with self.psycopg.connect(self.admin_dsn) as connection:
            activation_count, lease_count = connection.execute(
                """
                SELECT
                    (SELECT count(*) FROM public.mcp_rollout_deployment_activation
                     WHERE environment_id = %s),
                    (SELECT count(*) FROM public.mcp_rollout_instance_config
                     WHERE environment_id = %s)
                """,
                (self.environment_id, self.environment_id),
            ).fetchone()
        self.assertEqual((activation_count, lease_count), (2, 2))

    def test_resolution_is_unique_and_does_not_auto_activate(self) -> None:
        blocked_evidence = self._admin_production_evidence(
            evidence_id="prod-resolution-blocked",
            deployment_id="deploy-resolution-source",
            stage=MCPRolloutStage.INTERNAL_SHADOW,
            config_fingerprint="6" * 64,
            snapshot_id=1,
            offset_seconds=0,
        )
        block = asyncio.run(
            self._storage("evaluator").append_mcp_rollout_promotion_block(
                MCPRolloutPromotionBlock(
                    block_id="block-resolution",
                    environment_id=self.environment_id,
                    deployment_id=blocked_evidence.deployment_id,
                    stage=blocked_evidence.stage.value,
                    config_fingerprint=blocked_evidence.config_fingerprint,
                    evidence_id=blocked_evidence.evidence_id,
                    reason_code="window_incomplete",
                    created_at=self.now + timedelta(seconds=1),
                )
            )
        )
        healthy_evidence = self._admin_production_evidence(
            evidence_id="prod-resolution-healthy",
            deployment_id=blocked_evidence.deployment_id,
            stage=MCPRolloutStage.INTERNAL_SHADOW,
            config_fingerprint=blocked_evidence.config_fingerprint,
            snapshot_id=2,
            offset_seconds=10,
        )
        operator = self._storage("operator")
        approval = asyncio.run(
            operator.append_mcp_rollout_stage_approval(
                MCPRolloutStageApproval(
                    approval_id="approval-resolution",
                    environment_id=self.environment_id,
                    deployment_id="deploy-resolution-target",
                    stage="internal_enforce",
                    config_fingerprint="7" * 64,
                    evidence_id=healthy_evidence.evidence_id,
                    reason="verified remediation",
                    approver="operator",
                    created_at=self.now + timedelta(seconds=11),
                )
            )
        )
        resolution = MCPRolloutBlockResolution(
            resolution_id="resolution-once",
            block_id=block.block_id,
            approval_id=approval.approval_id,
            evidence_id=healthy_evidence.evidence_id,
            reason="remediation evidence reviewed",
            approver="operator",
            created_at=self.now + timedelta(seconds=12),
        )
        asyncio.run(operator.append_mcp_rollout_block_resolution(resolution))
        with self.assertRaises(DBAPIError):
            asyncio.run(
                operator.append_mcp_rollout_block_resolution(
                    replace(resolution, resolution_id="resolution-replay")
                )
            )
        with self.assertRaisesRegex(DBAPIError, "consumed by a block resolution"):
            asyncio.run(
                operator.activate_mcp_rollout_deployment(
                    MCPRolloutDeploymentActivation(
                        activation_id="activation-after-resolution",
                        environment_id=self.environment_id,
                        deployment_id=approval.deployment_id,
                        stage=approval.stage,
                        config_fingerprint=approval.config_fingerprint,
                        approval_id=approval.approval_id,
                        evidence_id=approval.evidence_id,
                        previous_activation_id=None,
                        operator_reason="resolution must not auto activate",
                        is_rollback=False,
                        created_at=self.now + timedelta(seconds=13),
                    )
                )
            )
        with self.psycopg.connect(self.admin_dsn) as connection:
            activation_count = connection.execute(
                """
                SELECT count(*)
                FROM public.mcp_rollout_deployment_activation
                WHERE environment_id = %s
                """,
                (self.environment_id,),
            ).fetchone()[0]
        self.assertEqual(activation_count, 0)

    @property
    def _deployment_id(self) -> str:
        return "deploy-shadow"

    def _dsn(self, role: str) -> str:
        return self._admin_url.set(
            drivername="postgresql+psycopg",
            username=self.login_names[role],
            password=ROLE_PASSWORD,
        ).render_as_string(hide_password=False)

    def _connection(self, role: str):
        return self.psycopg.connect(
            **{
                **self._admin_conninfo,
                "user": self.login_names[role],
                "password": ROLE_PASSWORD,
            }
        )

    def _engine(self, role: str):
        engine = create_postgres_engine(self._dsn(role), pool_size=1, max_overflow=0)
        self.engines.append(engine)
        return engine

    def _storage(self, role: str) -> PostgreSQLStorage:
        engine = self._engine(role)
        factory = create_postgres_session_factory(engine)
        return PostgreSQLStorage(
            factory,
            mcp_rollout_session_factory=factory,
            mcp_rollout_role=role,
        )

    def _execute_metric_direct(
        self,
        connection,
        bucket: MCPRolloutMetricBucket,
        *,
        additive: bool = True,
    ):
        function_name = "upsert_metric_bucket" if additive else "set_metric_bucket"
        cursor = connection.execute(
            f"""
            SELECT result.*
            FROM mcp_rollout_api.{function_name}(
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            ) AS result
            """,
            (
                bucket.metric_bucket_id,
                bucket.environment_id,
                bucket.deployment_id,
                bucket.stage,
                bucket.config_fingerprint,
                bucket.metric_name,
                bucket.bucket_started_at,
                bucket.bucket_ended_at,
                bucket.execution_path,
                bucket.routing_mode,
                bucket.transport,
                bucket.protocol_version,
                bucket.adapter,
                bucket.result_category,
                bucket.error_category,
                bucket.call_kind or "not_applicable",
                bucket.red_line or "not_applicable",
                bucket.latency_bucket,
                bucket.value,
                bucket.updated_at or bucket.created_at,
            ),
        )
        return dict(zip((item.name for item in cursor.description), cursor.fetchone()))

    def _execute_sample_direct(self, connection, sample: MCPShadowAuditSample):
        cursor = connection.execute(
            """
            SELECT result.*
            FROM mcp_rollout_api.append_shadow_audit_sample(
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s
            ) AS result
            """,
            (
                sample.sample_id,
                sample.environment_id,
                sample.deployment_id,
                sample.config_fingerprint,
                sample.manifest_fingerprint,
                sample.fixture_fingerprint,
                sample.mapping_fingerprint,
                sample.scenario,
                sample.nonce,
                sample.safe_owner_ref,
                sample.safe_task_ref,
                sample.safe_call_ref,
                sample.legacy_outcome,
                sample.shadow_outcome,
                sample.transport,
                sample.endpoint_policy,
                sample.comparison,
                json.dumps(sample.blockers),
                sample.payload_digest,
                sample.observed_at,
                sample.recorded_at,
                sample.expires_at,
            ),
        )
        return dict(zip((item.name for item in cursor.description), cursor.fetchone()))

    def _execute_drill_direct(
        self, connection, observation: MCPRolloutDrillObservation
    ):
        cursor = connection.execute(
            """
            SELECT result.*
            FROM mcp_rollout_api.append_drill_observation(
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            ) AS result
            """,
            (
                observation.drill_observation_id,
                observation.environment_id,
                observation.deployment_id,
                observation.config_fingerprint,
                observation.drill,
                observation.outcome,
                observation.observed_at,
                observation.recorded_at,
                observation.expires_at,
                observation.payload_digest,
            ),
        )
        return dict(zip((item.name for item in cursor.description), cursor.fetchone()))

    def _save_metric(self, bucket: MCPRolloutMetricBucket) -> None:
        asyncio.run(self._storage("app").upsert_mcp_rollout_metric_bucket(bucket))

    def _seed_required_metrics(
        self,
        *,
        deployment_id: str,
        stage: str,
        config_fingerprint: str,
        window_started_at: datetime,
        window_ended_at: datetime,
        call_kinds: tuple[str, ...],
        red_line_intervals: dict[str, tuple[tuple[datetime, datetime], ...]]
        | None = None,
    ) -> None:
        storage = self._storage("app")
        minute_intervals = tuple(
            (
                window_started_at + timedelta(minutes=offset),
                window_started_at + timedelta(minutes=offset + 1),
            )
            for offset in range(
                int((window_ended_at - window_started_at).total_seconds() // 60)
            )
        )
        intervals_by_red_line = red_line_intervals or {
            red_line: minute_intervals for red_line in RED_LINES
        }
        for red_line, intervals in intervals_by_red_line.items():
            for bucket_started_at, bucket_ended_at in intervals:
                asyncio.run(
                    storage.upsert_mcp_rollout_metric_bucket(
                        replace(
                            self._bucket(value=0),
                            metric_bucket_id=f"redline-{red_line}-{uuid4().hex}",
                            deployment_id=deployment_id,
                            stage=stage,
                            config_fingerprint=config_fingerprint,
                            metric_name="mcp_safety_red_line_total",
                            bucket_started_at=bucket_started_at,
                            bucket_ended_at=bucket_ended_at,
                            execution_path="not_applicable",
                            routing_mode="enforce",
                            adapter="not_applicable",
                            result_category="failed",
                            error_category="validation",
                            call_kind=None,
                            red_line=red_line,
                            value=0,
                            created_at=bucket_started_at,
                            updated_at=bucket_ended_at,
                        )
                    )
                )
        for call_kind in call_kinds:
            for result_category in TERMINAL_RESULTS:
                for index, (bucket_started_at, bucket_ended_at) in enumerate(
                    minute_intervals
                ):
                    asyncio.run(
                        storage.upsert_mcp_rollout_metric_bucket(
                            replace(
                                self._bucket(value=0),
                                metric_bucket_id=(
                                    f"terminal-{call_kind}-{result_category}-{uuid4().hex}"
                                ),
                                deployment_id=deployment_id,
                                stage=stage,
                                config_fingerprint=config_fingerprint,
                                metric_name="mcp_tool_calls_total",
                                bucket_started_at=bucket_started_at,
                                bucket_ended_at=bucket_ended_at,
                                execution_path="user_scoped",
                                routing_mode="enforce",
                                adapter="python_2026",
                                result_category=result_category,
                                error_category=(
                                    "none"
                                    if result_category == "succeeded"
                                    else "unknown"
                                ),
                                call_kind=call_kind,
                                red_line=None,
                                value=(
                                    1
                                    if index == 0
                                    and result_category == "succeeded"
                                    else 0
                                ),
                                created_at=bucket_started_at,
                                updated_at=bucket_ended_at,
                            )
                        )
                    )

    @staticmethod
    def _drill_names() -> tuple[str, ...]:
        return (
            "cancellation",
            "long_call_120_seconds",
            "disconnect_five_minutes",
            "restart_unknown",
            "mrtr_recovery",
            "tasks_recovery",
            "fair_queueing",
            "flag_rollback",
        )

    def _seed_passed_drills(
        self, *, deployment_id: str, config_fingerprint: str
    ) -> None:
        storage = self._storage("drill")
        for drill in self._drill_names():
            observation = seal_mcp_rollout_drill_observation(
                MCPRolloutDrillObservation(
                    drill_observation_id=f"drill-{drill}-{uuid4().hex}",
                    environment_id=self.environment_id,
                    deployment_id=deployment_id,
                    config_fingerprint=config_fingerprint,
                    drill=drill,
                    outcome="passed",
                    observed_at=self.now,
                    recorded_at=self.now,
                    expires_at=self.now + timedelta(days=3),
                    payload_digest="",
                )
            )
            asyncio.run(storage.append_mcp_rollout_drill_observation(observation))

    def _prepare_payload(
        self,
        *,
        deployment_id: str,
        window_started_at: datetime,
        window_ended_at: datetime,
    ) -> dict[str, object]:
        with self._connection("snapshot") as connection:
            cursor = connection.execute(
                """
                SELECT * FROM mcp_rollout_api.prepare_production_evidence_snapshot(
                    %s, %s, %s, %s, %s
                )
                """,
                (
                    self.environment_id,
                    deployment_id,
                    "a" * 40,
                    window_started_at,
                    window_ended_at,
                ),
            )
            row = dict(
                zip((item.name for item in cursor.description), cursor.fetchone())
            )
        return row["payload"]

    def _execute_activation_direct(
        self, connection, activation: MCPRolloutDeploymentActivation
    ) -> None:
        connection.execute(
            """
            SELECT result.*
            FROM mcp_rollout_api.append_deployment_activation(
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            ) AS result
            """,
            (
                activation.activation_id,
                activation.environment_id,
                activation.deployment_id,
                activation.stage,
                activation.config_fingerprint,
                activation.approval_id,
                activation.evidence_id,
                activation.previous_activation_id,
                activation.operator_reason,
                activation.is_rollback,
                activation.created_at,
            ),
        ).fetchone()

    def _execute_block_direct(
        self, connection, block: MCPRolloutPromotionBlock
    ) -> None:
        connection.execute(
            """
            SELECT result.*
            FROM mcp_rollout_api.append_promotion_block(
                %s, %s, %s, %s, %s, %s, %s, %s
            ) AS result
            """,
            (
                block.block_id,
                block.environment_id,
                block.deployment_id,
                block.stage,
                block.config_fingerprint,
                block.evidence_id,
                block.reason_code,
                block.created_at,
            ),
        ).fetchone()

    def _wait_for_lock(self, application_name: str) -> bool:
        deadline = time.monotonic() + 2
        with self.psycopg.connect(self.admin_dsn, autocommit=True) as connection:
            while time.monotonic() < deadline:
                waiting = connection.execute(
                    """
                    SELECT wait_event_type = 'Lock'
                    FROM pg_catalog.pg_stat_activity
                    WHERE application_name = %s
                    """,
                    (application_name,),
                ).fetchone()
                if waiting is not None and waiting[0]:
                    return True
                time.sleep(0.02)
        return False

    def _admin_production_evidence(
        self,
        *,
        evidence_id: str,
        deployment_id: str,
        stage: MCPRolloutStage,
        config_fingerprint: str,
        snapshot_id: int,
        offset_seconds: int,
    ) -> MCPEvidenceSnapshot:
        recorded_at = self.now + timedelta(seconds=offset_seconds)
        snapshot = MCPEvidenceSnapshot.seal(
            evidence_id=evidence_id,
            environment_id=self.environment_id,
            git_sha="a" * 40,
            deployment_id=deployment_id,
            stage=stage,
            config_fingerprint=config_fingerprint,
            window_started_at=recorded_at - timedelta(minutes=2),
            window_ended_at=recorded_at - timedelta(minutes=1),
            recorded_at=recorded_at,
            producer=MCPEvidenceProducer.PRODUCTION_SNAPSHOT,
            source=MCPEvidenceSource.PRODUCTION,
            snapshot_id=snapshot_id,
            nonce=f"nonce-{evidence_id}",
            payload=MCPRolloutEvidencePayload(kind=MCPEvidenceKind(stage.value)),
            attestation_key_id="integration-admin-key",
            attestation_key=b"phase3-admin-fixture-key",
        )
        payload = {
            "kind": stage.value,
            "metric_buckets": [],
            "call_kinds": [],
            "shadow_scenarios": [],
            "completed_drills": [],
            "red_line_counts": [],
            "continuous_window": False,
            "missing_bucket_count": 0,
            "invalid_evidence_count": 0,
            "unresolved_mismatch_count": 0,
            "unapproved_not_comparable_count": 0,
            "shadow_observation_count": 0,
            "pre_dispatch_excluded_count": 0,
            "ci_conformance_passed": False,
            "manifest_fingerprint": None,
            "fixture_fingerprint": None,
            "mapping_fingerprint": None,
        }
        with self.psycopg.connect(self.admin_dsn) as connection:
            connection.execute(
                """
                INSERT INTO public.mcp_rollout_evidence_snapshot (
                    evidence_id, environment_id, rollout_program, git_sha,
                    deployment_id, stage, config_fingerprint, window_started_at,
                    window_ended_at, recorded_at, producer, source, snapshot_id,
                    nonce, evidence_kind, payload, payload_digest,
                    attestation_key_id, attestation_signature
                ) VALUES (
                    %s, %s, 'user_mcp_phase3', %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s
                )
                """,
                (
                    snapshot.evidence_id,
                    snapshot.environment_id,
                    snapshot.git_sha,
                    snapshot.deployment_id,
                    snapshot.stage.value,
                    snapshot.config_fingerprint,
                    snapshot.window_started_at,
                    snapshot.window_ended_at,
                    snapshot.recorded_at,
                    snapshot.producer.value,
                    snapshot.source.value,
                    snapshot.snapshot_id,
                    snapshot.nonce,
                    snapshot.payload.kind.value,
                    json.dumps(payload, separators=(",", ":"), sort_keys=True),
                    snapshot.payload_digest,
                    snapshot.attestation_key_id,
                    snapshot.attestation_signature,
                ),
            )
        return snapshot

    def _insert_active_stage(
        self,
        *,
        deployment_id: str,
        stage: str,
        config_fingerprint: str,
    ) -> None:
        with self.psycopg.connect(self.admin_dsn) as connection:
            connection.execute(
                """
                INSERT INTO public.mcp_rollout_deployment_activation (
                    activation_id, environment_id, rollout_program,
                    deployment_id, stage, config_fingerprint, approval_id,
                    evidence_id, previous_activation_id, operator_reason,
                    is_rollback, created_at
                ) VALUES (
                    %s, %s, 'user_mcp_phase3', %s, %s, %s, %s, %s,
                    NULL, 'integration active-stage fixture', false, %s
                )
                """,
                (
                    f"activation-materialize-{uuid4().hex}",
                    self.environment_id,
                    deployment_id,
                    stage,
                    config_fingerprint,
                    f"approval-materialize-{uuid4().hex}",
                    f"evidence-materialize-{uuid4().hex}",
                    self.now - timedelta(minutes=3),
                ),
            )

    def _bucket(self, *, value: int) -> MCPRolloutMetricBucket:
        return MCPRolloutMetricBucket(
            metric_bucket_id="metric-1",
            environment_id=self.environment_id,
            deployment_id=self._deployment_id,
            stage="internal_shadow",
            config_fingerprint="a" * 64,
            metric_name="mcp_route_requests_total",
            bucket_started_at=self.now - timedelta(minutes=1),
            bucket_ended_at=self.now,
            execution_path="legacy",
            routing_mode="shadow",
            transport="not_applicable",
            protocol_version="not_applicable",
            adapter="legacy_global_runtime",
            result_category="succeeded",
            error_category="none",
            latency_bucket="not_applicable",
            value=value,
            created_at=self.now,
            updated_at=self.now,
        )

    def _sample(
        self, *, sample_id: str = "sample-1", nonce: str = "nonce-1"
    ) -> MCPShadowAuditSample:
        return seal_shadow_audit_sample(
            MCPShadowAuditSample(
                sample_id=sample_id,
                environment_id=self.environment_id,
                deployment_id=self._deployment_id,
                stage="internal_shadow",
                config_fingerprint="a" * 64,
                manifest_fingerprint="b" * 64,
                fixture_fingerprint="c" * 64,
                mapping_fingerprint="d" * 64,
                scenario="https_streamable_success",
                nonce=nonce,
                safe_owner_ref="hmac-sha256:" + "1" * 64,
                safe_task_ref="hmac-sha256:" + "2" * 64,
                safe_call_ref="hmac-sha256:" + "3" * 64,
                legacy_outcome="tool_call_succeeded",
                shadow_outcome="control_plane_ready",
                transport="streamable_http",
                endpoint_policy="runtime_enforced",
                comparison="matched",
                blockers=(),
                payload_digest="",
                observed_at=self.now,
                recorded_at=self.now,
                expires_at=self.now + timedelta(days=30),
            )
        )

    def _append_sample_direct(self, sample: MCPShadowAuditSample) -> None:
        with self._connection("app") as connection:
            connection.execute(
                """
                SELECT mcp_rollout_api.append_shadow_audit_sample(
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s
                )
                """,
                (
                    sample.sample_id,
                    sample.environment_id,
                    sample.deployment_id,
                    sample.config_fingerprint,
                    sample.manifest_fingerprint,
                    sample.fixture_fingerprint,
                    sample.mapping_fingerprint,
                    sample.scenario,
                    sample.nonce,
                    sample.safe_owner_ref,
                    sample.safe_task_ref,
                    sample.safe_call_ref,
                    sample.legacy_outcome,
                    sample.shadow_outcome,
                    sample.transport,
                    sample.endpoint_policy,
                    sample.comparison,
                    None if sample.blockers is None else json.dumps(sample.blockers),
                    sample.payload_digest,
                    sample.observed_at,
                    sample.recorded_at,
                    sample.expires_at,
                ),
            )

    def _ci_evidence(
        self,
        *,
        evidence_id: str | None = None,
        nonce: str | None = None,
    ) -> MCPEvidenceSnapshot:
        return MCPEvidenceSnapshot.seal(
            evidence_id=evidence_id or f"ci-{uuid4().hex}",
            environment_id=self.environment_id,
            git_sha="a" * 40,
            deployment_id="ci-source",
            stage=MCPRolloutStage.OFF,
            config_fingerprint="f" * 64,
            window_started_at=self.now - timedelta(minutes=2),
            window_ended_at=self.now - timedelta(minutes=1),
            recorded_at=self.now,
            producer=MCPEvidenceProducer.CI_PIPELINE,
            source=MCPEvidenceSource.CI,
            snapshot_id=1,
            nonce=nonce or f"nonce-{uuid4().hex}",
            payload=MCPRolloutEvidencePayload(
                kind=MCPEvidenceKind.CI_CONFORMANCE,
                ci_conformance_passed=True,
            ),
        )

    def _gate_scope(self, *, created_at: datetime) -> MCPRolloutGateScope:
        return MCPRolloutGateScope(
            environment_id=self.environment_id,
            created_at=created_at,
        )


if __name__ == "__main__":
    unittest.main()
