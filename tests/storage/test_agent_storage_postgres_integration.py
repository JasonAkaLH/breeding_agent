from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import delete, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from src.orchestration.agent_loop.models import (
    AgentFinishMetadata,
    AgentModelBinding,
    AgentRun,
    AgentRunStatus,
    AgentSample,
    AgentSampleCommit,
    AgentStorageConflict,
    AgentUsage,
)
from src.storage.postgres import (
    PostgreSQLAgentRepository,
    create_postgres_engine,
    create_postgres_session_factory,
)
from src.storage.sqlite.base import SQLiteBase
from src.storage.sqlite.models import AgentFinalReceiptRow, AgentItemRow, AgentRunRow, TaskNodeRow, TaskRow
from tests.postgres_test_support import isolated_postgres_test_dsn_or_skip_reason


class AgentStoragePostgresIntegrationTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dsn, skip_reason = isolated_postgres_test_dsn_or_skip_reason(
            "MAF_POSTGRES_AGENT_TEST_DSN",
            fallback_env="MAF_POSTGRES_TEST_DSN",
        )
        if skip_reason:
            raise unittest.SkipTest(skip_reason)
        assert cls.dsn is not None
        cls.engine = create_postgres_engine(cls.dsn)
        SQLiteBase.metadata.create_all(cls.engine)
        cls.session_factory = create_postgres_session_factory(cls.engine)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.dispose()

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        suffix = uuid4().hex
        self.task_id = f"agent-pg-task-{suffix}"
        self.run_id = f"agent-pg-run-{suffix}"
        self.conversation_id = f"agent-pg-conv-{suffix}"
        self.repository = PostgreSQLAgentRepository(self.session_factory)
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            session.add(
                TaskRow(
                    task_id=self.task_id,
                    conversation_id=self.conversation_id,
                    root_message_id=f"message-{suffix}",
                    status="accepted",
                    routing_mode="auto",
                    requested_capability_id=None,
                    root_node_id=None,
                    summary=None,
                    cancel_requested_at=None,
                    created_at=now,
                    updated_at=now,
                    mcp_execution_mode=None,
                    mcp_shadow_enabled=None,
                    mcp_rollout_config_version=None,
                    mcp_route_reason_code=None,
                    mcp_rollout_mode=None,
                )
            )
            session.commit()

    async def asyncTearDown(self) -> None:
        with self.session_factory() as session:
            session.execute(delete(AgentFinalReceiptRow).where(AgentFinalReceiptRow.run_id == self.run_id))
            session.execute(delete(AgentItemRow).where(AgentItemRow.run_id == self.run_id))
            session.execute(delete(AgentRunRow).where(AgentRunRow.run_id == self.run_id))
            session.execute(delete(TaskNodeRow).where(TaskNodeRow.task_id == self.task_id))
            session.execute(delete(TaskRow).where(TaskRow.task_id == self.task_id))
            session.commit()
        await super().asyncTearDown()

    async def test_schema_transaction_storage_clock_and_fencing_match_sqlite_contract(self) -> None:
        binding = AgentModelBinding("edition-pg")
        run = await self.repository.create_run(
            AgentRun(self.run_id, self.task_id, self.conversation_id, AgentRunStatus.RUNNING, binding)
        )
        lease = await self.repository.acquire_task_lease(
            self.run_id, owner_id="worker-a", ttl_seconds=30
        )
        self.assertGreater(lease.expires_at, datetime.now(timezone.utc))
        sample = AgentSample(
            "sample-pg",
            binding,
            "answer",
            (),
            AgentUsage(),
            AgentFinishMetadata("stop", 1),
        )
        with self.assertRaises(AgentStorageConflict):
            await self.repository.commit_agent_sample(
                AgentSampleCommit(self.run_id, run.revision, None, sample, {})
            )
        committed = await self.repository.commit_agent_sample(
            AgentSampleCommit(self.run_id, lease.revision, lease.token, sample, {})
        )
        self.assertEqual(committed.run.claim_token, lease.token)

    async def test_postgres_concurrent_lease_acquire_has_one_winner(self) -> None:
        await self.repository.create_run(
            AgentRun(self.run_id, self.task_id, self.conversation_id, AgentRunStatus.RUNNING, AgentModelBinding("edition-pg"))
        )
        results = await asyncio.gather(
            self.repository.acquire_task_lease(self.run_id, owner_id="worker-a", ttl_seconds=30),
            self.repository.acquire_task_lease(self.run_id, owner_id="worker-b", ttl_seconds=30),
            return_exceptions=True,
        )
        self.assertEqual(sum(not isinstance(result, Exception) for result in results), 1)
        self.assertEqual(sum(isinstance(result, AgentStorageConflict) for result in results), 1)

    async def test_postgres_transaction_fault_is_all_or_zero(self) -> None:
        binding = AgentModelBinding("edition-pg")
        await self.repository.create_run(
            AgentRun(self.run_id, self.task_id, self.conversation_id, AgentRunStatus.RUNNING, binding)
        )

        def fail(stage: str) -> None:
            if stage == "sample_after_items":
                raise RuntimeError("injected")

        repository = PostgreSQLAgentRepository(self.session_factory, fault_injector=fail)
        sample = AgentSample(
            "sample-pg-fault", binding, "answer", (), AgentUsage(), AgentFinishMetadata("stop", 1)
        )
        with self.assertRaisesRegex(RuntimeError, "injected"):
            await repository.commit_agent_sample(
                AgentSampleCommit(self.run_id, 0, None, sample, {})
            )
        self.assertEqual(await self.repository.list_items(self.run_id), ())
        self.assertEqual((await self.repository.get_run(self.run_id)).revision, 0)

    async def test_minimum_agent_role_can_write_agent_tables_but_not_sensitive_authority(self) -> None:
        suffix = uuid4().hex[:12]
        role = f"agent_test_{suffix}"
        password = f"agent-role-{suffix}"
        quoted_role = f'"{role}"'
        with self.engine.begin() as connection:
            connection.execute(text(f"CREATE ROLE {quoted_role} LOGIN PASSWORD '{password}'"))
            connection.execute(text(f"GRANT USAGE ON SCHEMA public TO {quoted_role}"))
            connection.execute(
                text(
                    "GRANT SELECT, INSERT, UPDATE ON agent_run, agent_item, agent_final_receipt, "
                    "task, task_node, artifact, message, event_record TO " + quoted_role
                )
            )
        role_dsn = make_url(self.dsn).set(username=role, password=password).render_as_string(hide_password=False)
        role_engine = create_postgres_engine(role_dsn)
        try:
            role_repository = PostgreSQLAgentRepository(
                create_postgres_session_factory(role_engine)
            )
            created = await role_repository.create_run(
                AgentRun(self.run_id, self.task_id, self.conversation_id, AgentRunStatus.RUNNING, AgentModelBinding("edition-pg"))
            )
            self.assertEqual(created.task_id, self.task_id)
            with role_engine.connect() as connection:
                with self.assertRaises(DBAPIError):
                    connection.execute(text("SELECT server_id FROM user_mcp_server LIMIT 1"))
        finally:
            role_engine.dispose()
            with self.engine.begin() as connection:
                connection.execute(text(f"DROP OWNED BY {quoted_role}"))
                connection.execute(text(f"DROP ROLE {quoted_role}"))
