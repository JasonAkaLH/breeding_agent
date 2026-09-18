from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
from threading import Barrier
import unittest
from uuid import uuid4

import psycopg
from sqlalchemy import event, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from src.core.models import MCPRolloutInstanceConfigLease
from src.integrations.mcp.rollout import MCPRolloutConfig, MCPRoutingMode
from src.storage.postgres import (
    PostgreSQLStorage,
    create_postgres_engine,
    create_postgres_session_factory,
)
from src.storage.postgres.bootstrap import bootstrap_postgres_database
from src.storage.postgres.mcp_first_enablement import (
    FirstEnablementRequest,
    MCPFirstEnablementError,
    initialize_mcp_first_production,
)
from src.storage.sqlalchemy_models import (
    ConversationRow,
    TaskRow,
    UserMCPServerRow,
    MCPRolloutPromotionBlockRow,
)

ROOT = Path(__file__).resolve().parents[2]
DSN_ENV = "MAF_POSTGRES_FIRST_ENABLEMENT_TEST_DSN"
LEDGER = (
    "mcp_rollout_gate_scope",
    "mcp_rollout_evidence_snapshot",
    "mcp_rollout_stage_approval",
    "mcp_rollout_deployment_activation",
)


class FirstEnablementPostgresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dsn = os.environ.get(DSN_ENV)
        if not cls.dsn:
            raise unittest.SkipTest(
                "isolated_postgres_first_enablement_dsn_not_configured"
            )
        cls.url = make_url(cls.dsn)
        suffix = uuid4().hex[:12]
        cls.template = "first_enable_template_" + suffix
        cls.app_login = "first_enable_app_" + suffix
        cls.password = uuid4().hex
        with psycopg.connect(cls.dsn, autocommit=True) as conn:
            conn.execute(f'CREATE DATABASE "{cls.template}"')
        engine = create_postgres_engine(
            cls.url.set(
                drivername="postgresql+psycopg", database=cls.template
            ).render_as_string(hide_password=False)
        )
        bootstrap_postgres_database(engine)
        engine.dispose()
        with psycopg.connect(
            cls.url.set(database=cls.template).render_as_string(hide_password=False),
            autocommit=True,
        ) as conn:
            conn.execute(
                (ROOT / "scripts/postgres/user_mcp_rollout_permissions.sql").read_text()
            )
            conn.execute(
                f"CREATE ROLE \"{cls.app_login}\" LOGIN PASSWORD '{cls.password}'"
            )
            conn.execute(
                f'GRANT maf_rollout_app_writer TO "{cls.app_login}" WITH INHERIT TRUE, SET FALSE'
            )

    @classmethod
    def tearDownClass(cls):
        with psycopg.connect(cls.dsn, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE "{cls.template}" WITH (FORCE)')
            conn.execute(f'DROP ROLE "{cls.app_login}"')

    def setUp(self):
        self.database = "first_enable_case_" + uuid4().hex[:12]
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            conn.execute(
                f'CREATE DATABASE "{self.database}" TEMPLATE "{self.template}"'
            )
        url = self.url.set(drivername="postgresql+psycopg", database=self.database)
        self.admin = create_postgres_engine(url.render_as_string(hide_password=False))
        self.app = create_postgres_engine(
            url.set(username=self.app_login, password=self.password).render_as_string(
                hide_password=False
            )
        )
        self.request = FirstEnablementRequest(
            self.database,
            "prod",
            "release-1",
            "activation-1",
            "a" * 40,
            "sha256:" + "b" * 64,
        )
        self.config = MCPRolloutConfig(
            gateway_enabled=True,
            routing_mode=MCPRoutingMode.ENFORCE,
            legacy_enabled=False,
            enforce_percent=100,
            enforce_hash_salt="test-salt",
        )

    def tearDown(self):
        self.admin.dispose()
        self.app.dispose()
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE "{self.database}" WITH (FORCE)')

    def check(self, **kwargs):
        return initialize_mcp_first_production(
            self.admin,
            self.app,
            kwargs.pop("request", self.request),
            kwargs.pop("config", self.config),
            **kwargs,
        )

    def apply(self):
        report = self.check()
        return self.check(apply=True, expected_check_digest=report["check_digest"])

    def assert_ledger_empty(self):
        with self.admin.connect() as conn:
            for table in LEDGER:
                self.assertEqual(
                    conn.scalar(text(f"SELECT count(*) FROM public.{table}")), 0
                )

    def add_server(self):
        with self.admin.begin() as conn:
            conn.execute(
                UserMCPServerRow.__table__.insert().values(
                    server_id="server-1",
                    owner_user_id="owner-1",
                    display_name="test",
                    routing_description="test",
                    endpoint_url="https://example.com/mcp",
                    transport="streamable_http",
                    protocol_preference="auto",
                    auth_type="none",
                    health_status="unknown",
                )
            )

    def test_readonly_apply_preserves_history_and_exact_retry_after_user_data(self):
        with self.admin.begin() as conn:
            conn.execute(
                ConversationRow.__table__.insert().values(
                    conversation_id="old-conversation",
                    username="existing",
                    status="active",
                )
            )
            conn.execute(
                TaskRow.__table__.insert().values(
                    task_id="old-task",
                    conversation_id="old-conversation",
                    root_message_id="old-message",
                    status="completed",
                    routing_mode="auto",
                )
            )
            before = conn.execute(select(ConversationRow.__table__)).mappings().all()
            before_tasks = conn.execute(select(TaskRow.__table__)).mappings().all()
        report = self.check()
        self.assertEqual(report["status"], "ready")
        self.assert_ledger_empty()
        self.assertEqual(
            self.check(apply=True, expected_check_digest=report["check_digest"])[
                "status"
            ],
            "initialized",
        )
        self.add_server()
        self.assertEqual(
            self.check(apply=True, expected_check_digest=report["check_digest"])[
                "status"
            ],
            "already_initialized",
        )
        with self.admin.connect() as conn:
            self.assertEqual(
                conn.execute(select(ConversationRow.__table__)).mappings().all(), before
            )
            self.assertEqual(
                conn.execute(select(TaskRow.__table__)).mappings().all(), before_tasks
            )
            self.assertEqual(
                conn.scalar(
                    text(
                        "SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE'"
                    )
                ),
                66,
            )
        for request in (
            replace(self.request, environment_id="other"),
            replace(self.request, deployment_id="other"),
            replace(self.request, backend_image_digest="sha256:" + "c" * 64),
            replace(self.request, git_sha="d" * 40),
        ):
            with (
                self.subTest(request=request),
                self.assertRaises(MCPFirstEnablementError),
            ):
                self.check(request=request)
        with self.assertRaises(MCPFirstEnablementError):
            self.check(config=replace(self.config, enforce_hash_salt="different"))

    def test_wrong_database_role_config_and_nonempty_state_rejected(self):
        for kwargs in (
            {"request": replace(self.request, database_name="other")},
            {"config": replace(self.config, legacy_enabled=True)},
            {"config": replace(self.config, enforce_percent=50, legacy_enabled=True)},
        ):
            with (
                self.subTest(kwargs=kwargs),
                self.assertRaises(MCPFirstEnablementError),
            ):
                self.check(**kwargs)
        with self.assertRaisesRegex(
            MCPFirstEnablementError, "real_superuser_login_required"
        ):
            initialize_mcp_first_production(
                self.app, self.app, self.request, self.config
            )
        with self.assertRaises(RuntimeError):
            initialize_mcp_first_production(
                self.admin, self.admin, self.request, self.config
            )
        self.add_server()
        with self.assertRaisesRegex(MCPFirstEnablementError, "already_exists"):
            self.check()
        self.assert_ledger_empty()

    def test_apply_rechecks_state_and_report(self):
        report = self.check()
        with self.assertRaisesRegex(MCPFirstEnablementError, "check_digest_mismatch"):
            self.check(apply=True, expected_check_digest="f" * 64)
        self.add_server()
        with self.assertRaisesRegex(MCPFirstEnablementError, "already_exists"):
            self.check(apply=True, expected_check_digest=report["check_digest"])
        self.assert_ledger_empty()

    def test_each_failed_write_rolls_back_every_record(self):
        report = self.check()
        for table in LEDGER[1:]:

            def fail(conn, cursor, statement, parameters, context, executemany):
                if statement.startswith("INSERT INTO " + table + " "):
                    raise RuntimeError("injected_write_failure")

            event.listen(self.admin, "before_cursor_execute", fail)
            try:
                with (
                    self.subTest(table=table),
                    self.assertRaisesRegex(RuntimeError, "injected_write_failure"),
                ):
                    self.check(apply=True, expected_check_digest=report["check_digest"])
            finally:
                event.remove(self.admin, "before_cursor_execute", fail)
            self.assert_ledger_empty()

    def test_concurrent_initializers_create_only_one_group(self):
        report = self.check()
        barrier = Barrier(2)

        def run():
            barrier.wait()
            try:
                return self.check(
                    apply=True, expected_check_digest=report["check_digest"]
                )["status"]
            except MCPFirstEnablementError as exc:
                return str(exc)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(run) for _ in range(2)]
            results = [f.result() for f in futures]
        self.assertEqual(results.count("initialized"), 1)
        self.assertTrue(
            set(results)
            <= {"initialized", "initialization_busy", "already_initialized"}
        )
        with self.admin.connect() as conn:
            for table in LEDGER:
                self.assertEqual(conn.scalar(text(f"SELECT count(*) FROM {table}")), 1)

    def test_normal_app_lease_restart_mismatch_and_safety_block(self):
        report = self.apply()
        factory = create_postgres_session_factory(self.app)
        storage = PostgreSQLStorage(
            factory, mcp_rollout_session_factory=factory, mcp_rollout_role="app"
        )
        now = datetime.now(timezone.utc)
        lease = MCPRolloutInstanceConfigLease(
            "lease-1",
            "prod",
            "release-1",
            "instance-1",
            "legacy_assembly_off",
            self.config.fingerprint,
            "activation-1",
            now + timedelta(minutes=5),
            now,
            now,
        )
        asyncio.run(storage.save_mcp_rollout_instance_config_lease(lease))
        asyncio.run(
            storage.save_mcp_rollout_instance_config_lease(
                replace(lease, updated_at=now + timedelta(seconds=1))
            )
        )
        with self.assertRaises(DBAPIError):
            asyncio.run(
                storage.save_mcp_rollout_instance_config_lease(
                    replace(lease, config_fingerprint="f" * 64)
                )
            )
        with self.assertRaises(DBAPIError):
            with self.app.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO mcp_rollout_gate_scope VALUES ('forged', 'user_mcp_phase3', CURRENT_TIMESTAMP)"
                    )
                )
        with self.admin.begin() as conn:
            conn.execute(
                MCPRolloutPromotionBlockRow.__table__.insert().values(
                    block_id="block-1",
                    environment_id="prod",
                    rollout_program="user_mcp_phase3",
                    deployment_id="release-1",
                    stage="legacy_assembly_off",
                    config_fingerprint=self.config.fingerprint,
                    evidence_id=report["evidence_id"],
                    reason_code="safety_red_line",
                    created_at=now,
                )
            )
        with self.assertRaises(DBAPIError):
            asyncio.run(
                storage.save_mcp_rollout_instance_config_lease(
                    replace(lease, updated_at=now + timedelta(seconds=2))
                )
            )
        with self.assertRaisesRegex(MCPFirstEnablementError, "safety_block"):
            self.check()

    def test_schema_drift_and_partial_records_fail_closed(self):
        with self.admin.begin() as conn:
            conn.execute(
                text(
                    "ALTER TABLE mcp_rollout_evidence_snapshot DROP CONSTRAINT ck_mcp_rollout_evidence_snapshot_mcp_rollout_evidence_source"
                )
            )
        with self.assertRaisesRegex(MCPFirstEnablementError, "schema_upgrade_required"):
            self.check()
        with psycopg.connect(
            self.url.set(database=self.database).render_as_string(hide_password=False),
            autocommit=True,
        ) as conn:
            sql = (
                ROOT / "scripts/postgres/user_mcp_first_enablement_constraints.sql"
            ).read_text()
            conn.execute(sql)
            conn.execute(sql)
        self.apply()
        # Fault fixture: simulate incomplete durable history without modifying production triggers.
        with self.admin.begin() as conn:
            conn.execute(text("SET LOCAL session_replication_role = replica"))
            conn.execute(text("DELETE FROM mcp_rollout_stage_approval"))
        with self.assertRaisesRegex(MCPFirstEnablementError, "partial_or_superseded"):
            self.check()

    def test_history_trigger_and_cross_database_app_are_rejected(self):
        other_app = create_postgres_engine(
            self.url.set(
                drivername="postgresql+psycopg",
                database=self.template,
                username=self.app_login,
                password=self.password,
            ).render_as_string(hide_password=False)
        )
        try:
            with self.assertRaisesRegex(
                MCPFirstEnablementError, "app_database_mismatch"
            ):
                initialize_mcp_first_production(
                    self.admin, other_app, self.request, self.config
                )
        finally:
            other_app.dispose()
        with self.admin.begin() as conn:
            conn.execute(
                text(
                    "ALTER TABLE mcp_rollout_evidence_snapshot DISABLE TRIGGER mcp_rollout_history_append_only"
                )
            )
        with self.assertRaisesRegex(
            MCPFirstEnablementError, "history_protection_required"
        ):
            self.check()
        self.assert_ledger_empty()

    def test_compatibility_sql_preserves_old_evidence_and_rejects_mixed_source(self):
        from dataclasses import asdict
        from src.integrations.mcp.observability import mcp_evidence_snapshot_to_record
        from src.integrations.mcp.rollout_evidence import (
            MCPEvidenceKind,
            MCPEvidenceProducer,
            MCPEvidenceSnapshot,
            MCPEvidenceSource,
            MCPRolloutEvidencePayload,
            MCPRolloutStage,
        )
        from src.storage.sqlalchemy_models import MCPRolloutEvidenceSnapshotRow

        table = MCPRolloutEvidenceSnapshotRow.__table__
        now = datetime.now(timezone.utc)
        snapshot = MCPEvidenceSnapshot.seal(
            evidence_id="old-ci",
            environment_id="prod",
            git_sha="a" * 40,
            deployment_id="old-release",
            stage=MCPRolloutStage.OFF,
            config_fingerprint="b" * 64,
            window_started_at=now - timedelta(minutes=1),
            window_ended_at=now,
            recorded_at=now,
            producer=MCPEvidenceProducer.CI_PIPELINE,
            source=MCPEvidenceSource.CI,
            snapshot_id=1,
            nonce="old-ci",
            payload=MCPRolloutEvidencePayload(
                kind=MCPEvidenceKind.CI_CONFORMANCE, ci_conformance_passed=True
            ),
        )
        row = asdict(mcp_evidence_snapshot_to_record(snapshot))
        with self.admin.begin() as conn:
            conn.execute(table.insert().values(row))
            # Reproduce the old source constraint; the explicit SQL must expand it.
            conn.execute(
                text(
                    "ALTER TABLE mcp_rollout_evidence_snapshot DROP CONSTRAINT ck_mcp_rollout_evidence_snapshot_mcp_rollout_evidence_source"
                )
            )
            conn.execute(
                text(
                    "ALTER TABLE mcp_rollout_evidence_snapshot ADD CONSTRAINT ck_mcp_rollout_evidence_snapshot_mcp_rollout_evidence_source CHECK (source IN ('ci', 'production'))"
                )
            )
        with self.assertRaisesRegex(MCPFirstEnablementError, "schema_upgrade_required"):
            self.check()
        with psycopg.connect(
            self.url.set(database=self.database).render_as_string(hide_password=False),
            autocommit=True,
        ) as conn:
            sql = (
                ROOT / "scripts/postgres/user_mcp_first_enablement_constraints.sql"
            ).read_text()
            conn.execute(sql)
            conn.execute(sql)
        with self.admin.connect() as conn:
            self.assertEqual(dict(conn.execute(select(table)).mappings().one()), row)
        for changes in (
            {"source": "deployment"},
            {"producer": "deployment_initializer"},
            {"evidence_kind": "first_enablement"},
        ):
            with self.subTest(changes=changes), self.assertRaises(DBAPIError):
                with self.admin.begin() as conn:
                    conn.execute(
                        table.insert().values(
                            {
                                **row,
                                "evidence_id": "bad",
                                "nonce": "bad",
                                "snapshot_id": 2,
                                **changes,
                            }
                        )
                    )
        with self.assertRaisesRegex(MCPFirstEnablementError, "already_exists"):
            self.check()

    def test_ordinary_postgres_writers_cannot_append_first_evidence_or_approval(self):
        from src.core.models import MCPRolloutStageApproval
        from src.integrations.mcp.observability import mcp_evidence_snapshot_to_record
        from tests.integrations.mcp.test_mcp_first_enablement_evidence import (
            first_snapshot,
        )

        factory = create_postgres_session_factory(self.app)
        storage = PostgreSQLStorage(
            factory, mcp_rollout_session_factory=factory, mcp_rollout_role="app"
        )
        with self.assertRaises(ValueError):
            asyncio.run(
                storage.append_mcp_rollout_evidence_snapshot(
                    mcp_evidence_snapshot_to_record(first_snapshot())
                )
            )
        report = self.apply()
        # Even the real superuser invoking the ordinary approval function cannot
        # use first-enablement evidence as CI/observational transition evidence.
        admin_factory = create_postgres_session_factory(self.admin)
        operator = PostgreSQLStorage(
            admin_factory,
            mcp_rollout_session_factory=admin_factory,
            mcp_rollout_role="operator",
        )
        with self.assertRaises(DBAPIError):
            asyncio.run(
                operator.append_mcp_rollout_stage_approval(
                    MCPRolloutStageApproval(
                        approval_id="second-approval",
                        environment_id="prod",
                        deployment_id="second-release",
                        stage="internal_shadow",
                        config_fingerprint=self.config.fingerprint,
                        evidence_id=report["evidence_id"],
                        reason="test",
                        approver="test",
                        created_at=datetime.now(timezone.utc),
                    )
                )
            )
