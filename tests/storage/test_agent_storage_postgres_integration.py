from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import delete

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
    bootstrap_postgres_database,
    create_postgres_engine,
    create_postgres_session_factory,
)
from src.storage.sqlite.models import AgentFinalReceiptRow, AgentItemRow, AgentRunRow, TaskNodeRow, TaskRow


class AgentStoragePostgresIntegrationTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dsn = os.environ.get("MAF_POSTGRES_TEST_DSN", "")
        if not cls.dsn:
            raise unittest.SkipTest("postgres_test_dsn_not_configured")
        cls.engine = create_postgres_engine(cls.dsn)
        bootstrap_postgres_database(cls.engine)
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
