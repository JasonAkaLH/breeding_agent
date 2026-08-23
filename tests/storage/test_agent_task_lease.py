from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.orchestration.agent_loop.lease import ACTIVE_LEASE_PHASES, AgentLeaseController
from src.orchestration.agent_loop.models import (
    AgentCallOutcomeCommit,
    AgentCallOutcomeStatus,
    AgentFinishMetadata,
    AgentLeaseLost,
    AgentModelBinding,
    AgentRun,
    AgentRunStatus,
    AgentSample,
    AgentSampleCommit,
    AgentStorageConflict,
    AgentTaskLease,
    AgentToolCall,
    AgentUsage,
)
from src.storage.sqlite import SQLiteAgentRepository, bootstrap_sqlite_database, create_sqlite_engine, create_sqlite_session_factory
from src.storage.sqlite.models import TaskRow


class SQLiteAgentTaskLeaseTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.engine = create_sqlite_engine(Path(self._tmpdir.name) / "lease.sqlite3")
        self.session_factory = create_sqlite_session_factory(self.engine)
        bootstrap_sqlite_database(self.engine)
        self.now = datetime(2026, 8, 22, 8, 0, tzinfo=timezone.utc)
        tokens = iter(("token-1", "token-2", "token-3", "token-4"))
        self.repository = SQLiteAgentRepository(
            self.session_factory,
            now_fn=lambda: self.now,
            token_factory=lambda: next(tokens),
        )
        with self.session_factory() as session:
            session.add(
                TaskRow(
                    task_id="task",
                    conversation_id="conv",
                    root_message_id="message",
                    status="accepted",
                    routing_mode="auto",
                    requested_capability_id=None,
                    summary=None,
                    cancel_requested_at=None,
                    created_at=self.now,
                    updated_at=self.now,
                    mcp_execution_mode=None,
                    mcp_shadow_enabled=None,
                    mcp_rollout_config_version=None,
                    mcp_route_reason_code=None,
                    mcp_rollout_mode=None,
                )
            )
            session.commit()
        await self.repository.create_run(
            AgentRun("run", "task", "conv", AgentRunStatus.RUNNING, AgentModelBinding("edition"))
        )

    async def asyncTearDown(self) -> None:
        self.engine.dispose()
        self._tmpdir.cleanup()
        await super().asyncTearDown()

    async def test_storage_clock_acquire_rotation_stale_commit_and_expiry_takeover(self) -> None:
        lease = await self.repository.acquire_task_lease("run", owner_id="worker-a", ttl_seconds=30)
        self.assertEqual(lease.expires_at, self.now + timedelta(seconds=30))
        with self.assertRaisesRegex(AgentStorageConflict, "held"):
            await self.repository.acquire_task_lease("run", owner_id="worker-b", ttl_seconds=30)

        self.now += timedelta(seconds=10)
        renewed = await self.repository.renew_task_lease(
            "run", owner_id="worker-a", token=lease.token, ttl_seconds=30
        )
        self.assertNotEqual(renewed.token, lease.token)
        self.assertEqual(renewed.revision, lease.revision + 1)
        sample = AgentSample(
            "sample",
            AgentModelBinding("edition"),
            "answer",
            (),
            AgentUsage(),
            AgentFinishMetadata("stop", 1),
        )
        with self.assertRaisesRegex(AgentStorageConflict, "cas_mismatch"):
            await self.repository.commit_agent_sample(
                AgentSampleCommit("run", lease.revision, lease.token, sample, {})
            )
        committed = await self.repository.commit_agent_sample(
            AgentSampleCommit("run", renewed.revision, renewed.token, sample, {})
        )
        self.assertEqual(committed.run.claim_token, renewed.token)

        self.now = renewed.expires_at + timedelta(microseconds=1)
        takeover = await self.repository.acquire_task_lease("run", owner_id="worker-b", ttl_seconds=30)
        self.assertEqual(takeover.owner_id, "worker-b")
        self.assertNotEqual(takeover.token, renewed.token)

    async def test_waiting_is_committed_before_release_and_resume_reacquires(self) -> None:
        lease = await self.repository.acquire_task_lease("run", owner_id="worker-a", ttl_seconds=30)
        sample = AgentSample(
            "sample",
            AgentModelBinding("edition"),
            "",
            (AgentToolCall("call", "tool", "{}", 0),),
            AgentUsage(),
            AgentFinishMetadata("tool_calls", 1),
        )
        committed = await self.repository.commit_agent_sample(
            AgentSampleCommit("run", lease.revision, lease.token, sample, {"tool": "skill.tool"})
        )
        with self.assertRaisesRegex(AgentStorageConflict, "release_rejected"):
            await self.repository.release_waiting_task_lease(
                "run", owner_id="worker-a", token=lease.token
            )
        await self.repository.commit_agent_call_outcome(
            AgentCallOutcomeCommit(
                "run",
                committed.run.revision,
                lease.token,
                committed.call_items[0].item_id,
                {"prompt": "value"},
                AgentCallOutcomeStatus.WAITING_FOR_INPUT,
            )
        )
        released = await self.repository.release_waiting_task_lease(
            "run", owner_id="worker-a", token=lease.token
        )
        self.assertEqual(released.status, AgentRunStatus.WAITING_FOR_INPUT)
        self.assertIsNone(released.claim_token)
        resumed = await self.repository.acquire_task_lease("run", owner_id="worker-b", ttl_seconds=30)
        self.assertEqual(resumed.owner_id, "worker-b")

    async def test_renew_at_or_after_expiry_fails_closed(self) -> None:
        lease = await self.repository.acquire_task_lease("run", owner_id="worker-a", ttl_seconds=30)
        self.now = lease.expires_at
        with self.assertRaisesRegex(AgentStorageConflict, "lease_lost"):
            await self.repository.renew_task_lease(
                "run", owner_id="worker-a", token=lease.token, ttl_seconds=30
            )

    async def test_concurrent_acquire_has_exactly_one_winner(self) -> None:
        results = await asyncio.gather(
            self.repository.acquire_task_lease("run", owner_id="worker-a", ttl_seconds=30),
            self.repository.acquire_task_lease("run", owner_id="worker-b", ttl_seconds=30),
            return_exceptions=True,
        )
        winners = [result for result in results if isinstance(result, AgentTaskLease)]
        losers = [result for result in results if isinstance(result, AgentStorageConflict)]
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(losers), 1)


class _FakeLeaseStore:
    def __init__(self, *, fail_renew: bool = False) -> None:
        self.fail_renew = fail_renew
        self.renew_count = 0
        self.current = AgentTaskLease(
            "run", "task", "worker", "token-1", 1, datetime.now(timezone.utc) + timedelta(seconds=30)
        )

    async def acquire_task_lease(self, run_id, *, owner_id, ttl_seconds):
        return self.current

    async def renew_task_lease(self, run_id, *, owner_id, token, ttl_seconds):
        self.renew_count += 1
        if self.fail_renew:
            raise AgentStorageConflict("lost")
        self.current = AgentTaskLease(
            "run", "task", "worker", f"token-{self.renew_count + 1}", self.renew_count + 1, self.current.expires_at + timedelta(seconds=30)
        )
        return self.current

    async def release_waiting_task_lease(self, run_id, *, owner_id, token):
        raise NotImplementedError


class AgentLeaseControllerTest(unittest.IsolatedAsyncioTestCase):
    async def test_positive_ttl_and_active_phases_use_ttl_over_three_heartbeat(self) -> None:
        with self.assertRaisesRegex(ValueError, "positive"):
            AgentLeaseController(_FakeLeaseStore(), ttl_seconds=0)
        sleep_values: list[float] = []

        async def sleep(seconds: float) -> None:
            sleep_values.append(seconds)
            await asyncio.sleep(0)

        store = _FakeLeaseStore()
        controller = AgentLeaseController(store, ttl_seconds=30, sleep=sleep)
        handle = await controller.acquire("run", owner_id="worker")

        async def operation(current_handle):
            starting_count = store.renew_count
            while store.renew_count == starting_count:
                await asyncio.sleep(0)
            return current_handle.current.token

        for phase in ACTIVE_LEASE_PHASES:
            token = await controller.run_active_phase(phase, handle, operation)
            self.assertTrue(token.startswith("token-"))
        self.assertTrue(all(value == 10 for value in sleep_values))
        self.assertGreaterEqual(store.renew_count, len(ACTIVE_LEASE_PHASES))

    async def test_heartbeat_loss_cancels_active_operation(self) -> None:
        store = _FakeLeaseStore(fail_renew=True)

        async def immediate_sleep(_seconds: float) -> None:
            await asyncio.sleep(0)

        controller = AgentLeaseController(store, ttl_seconds=30, sleep=immediate_sleep)
        handle = await controller.acquire("run", owner_id="worker")
        cancelled = asyncio.Event()

        async def operation(_handle):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        with self.assertRaises(AgentLeaseLost):
            await controller.run_active_phase("capability_wave", handle, operation)
        self.assertTrue(cancelled.is_set())
