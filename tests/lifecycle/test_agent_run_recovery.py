from __future__ import annotations

import unittest
from datetime import datetime, timezone

from src.orchestration.agent_loop.lease import AgentLeaseController
from src.orchestration.agent_loop.models import (
    AgentModelBinding,
    AgentRun,
    AgentRunStatus,
    AgentTaskLease,
)


class _WaitingLeaseStore:
    def __init__(self) -> None:
        self.renewals = 0
        self.releases = 0

    async def acquire_task_lease(self, run_id, *, owner_id, ttl_seconds):
        return AgentTaskLease(
            run_id=run_id,
            task_id="task-1",
            owner_id=owner_id,
            token="token-1",
            revision=1,
            expires_at=datetime(2026, 8, 22, tzinfo=timezone.utc),
        )

    async def renew_task_lease(self, *args, **kwargs):
        self.renewals += 1
        raise AssertionError("waiting lease must not heartbeat")

    async def release_waiting_task_lease(self, run_id, *, owner_id, token):
        self.releases += 1
        return AgentRun(
            run_id,
            "task-1",
            "conv-1",
            AgentRunStatus.WAITING_FOR_INPUT,
            AgentModelBinding("edition-a"),
        )


class AgentRunRecoveryLeaseTest(unittest.IsolatedAsyncioTestCase):
    async def test_waiting_release_uses_no_heartbeat_or_worker_phase(self) -> None:
        store = _WaitingLeaseStore()
        sleep_calls = 0

        async def sleep(_seconds):
            nonlocal sleep_calls
            sleep_calls += 1

        controller = AgentLeaseController(store, ttl_seconds=30, sleep=sleep)
        handle = await controller.acquire("run-1", owner_id="worker-1")

        released = await controller.release_waiting("run-1", handle=handle)

        self.assertEqual(released.status, AgentRunStatus.WAITING_FOR_INPUT)
        self.assertEqual(store.releases, 1)
        self.assertEqual(store.renewals, 0)
        self.assertEqual(sleep_calls, 0)
