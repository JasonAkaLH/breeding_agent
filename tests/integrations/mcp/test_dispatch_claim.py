from __future__ import annotations

import asyncio
import unittest
from datetime import timedelta
from time import monotonic
from pathlib import Path
from unittest.mock import AsyncMock

from src.core.contracts import CapabilityExecutionRequest
from src.core.errors import ExecutionOwnershipLost
from src.integrations.mcp.dispatch_coordinator import (
    UserMCPDispatchCoordinator,
    _DispatchAuthority,
)
from tests.storage import test_mcp_dispatch_aggregate_repository as aggregate


class DispatchClaimTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = aggregate.MCPDispatchAggregateRepositoryTest()
        await self.fixture.asyncSetUp()
        # Run borrowed fixture cleanups on this case's active asyncio runner.
        for callback, args, kwargs in self.fixture._cleanups:
            self.addCleanup(callback, *args, **kwargs)
        self.fixture._cleanups.clear()
        self.now = aggregate.NOW
        claimed = await self.fixture._claim()
        self.authority = _DispatchAuthority(
            self.fixture.intent_id,
            self.fixture.outbox_id,
            "worker",
            "token",
            revision=claimed.revision,
        )
        self.coordinator = object.__new__(UserMCPDispatchCoordinator)
        self.coordinator._storage = self.fixture.storage
        self.coordinator._now = lambda: self.now
        self.ticks = asyncio.Queue()
        self.renewed = asyncio.Queue()
        original_renew = self.coordinator._renew_dispatch_claim

        async def renew(authority):
            await original_renew(authority)
            self.renewed.put_nowait(None)

        async def interval(stop, *, delay=5.0):
            tick = asyncio.create_task(self.ticks.get())
            stopped = asyncio.create_task(stop.wait())
            try:
                done, _ = await asyncio.wait(
                    {tick, stopped}, return_when=asyncio.FIRST_COMPLETED
                )
                if tick in done:
                    self.now += timedelta(seconds=5)
            finally:
                tick.cancel()
                stopped.cancel()
                await asyncio.gather(tick, stopped, return_exceptions=True)

        self.coordinator._renew_dispatch_claim = renew
        self.coordinator._wait_claim_interval = interval

    async def _tick(self):
        self.ticks.put_nowait(None)
        await asyncio.wait_for(self.renewed.get(), 2)

    async def test_silent_selector_and_tool_share_one_continuous_lease(self):
        release_selector, release_tool = asyncio.Event(), asyncio.Event()
        tool_started = asyncio.Event()
        tool_calls = 0

        async def operation(*_args, **_kwargs):
            nonlocal tool_calls
            await release_selector.wait()
            tool_calls += 1
            tool_started.set()
            await release_tool.wait()
            return "completed"

        self.coordinator._terminal_result_root = Path(self.fixture.temporary.name)
        self.coordinator._resolve_identity = AsyncMock(
            return_value=("alice", "conversation-1", "message-1")
        )
        self.coordinator._prepare_dispatch_authority = AsyncMock(
            return_value=self.authority
        )
        self.coordinator._dispatch_active = operation
        worker = asyncio.create_task(
            self.coordinator.dispatch(
                CapabilityExecutionRequest(
                    "mcp.dispatch", "conversation-1", "task-1", "node-1"
                ),
                server_id="server-1",
            )
        )
        await asyncio.wait_for(self.renewed.get(), 2)
        # Selector silence exceeds the 10-second renewal window.
        for _ in range(3):
            await self._tick()
        release_selector.set()
        await asyncio.wait_for(tool_started.wait(), 2)
        # The single tool spans three 30-second TTLs, without real-time sleeps.
        for _ in range(19):
            await self._tick()
        outbox = await self.fixture.storage.get_mcp_dispatch_resume_outbox(
            self.fixture.outbox_id
        )
        self.assertEqual(outbox.lease_expires_at, self.now + timedelta(seconds=30))
        self.assertEqual(tool_calls, 1)
        release_tool.set()
        self.assertEqual(await asyncio.wait_for(worker, 2), "completed")
        before = self.authority.revision
        await asyncio.sleep(0)
        self.assertEqual(self.authority.revision, before)

    async def test_heartbeat_loss_cancels_waiting_tool_without_starting_another(self):
        started, cancelled = asyncio.Event(), asyncio.Event()
        calls = 0

        async def operation():
            nonlocal calls
            calls += 1
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        worker = asyncio.create_task(
            self.coordinator._run_claimed_dispatch(self.authority, operation)
        )
        await asyncio.wait_for(started.wait(), 2)
        await self.renewed.get()
        with self.fixture.sessions() as session:
            row = session.get(
                aggregate.MCPDispatchResumeOutboxRow, self.fixture.outbox_id
            )
            row.claim_owner, row.claim_token = "new", "new-token"
            row.revision += 1
            session.commit()
        self.ticks.put_nowait(None)
        with self.assertRaises(ExecutionOwnershipLost):
            await asyncio.wait_for(worker, 2)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(calls, 1)
        current = await self.fixture.storage.get_mcp_dispatch_resume_outbox(
            self.fixture.outbox_id
        )
        self.assertEqual(current.claim_token, "new-token")

    async def test_committed_release_stops_renewal_without_losing_ownership(self):
        await self.coordinator._renew_dispatch_claim(self.authority)

        async def finalize(current):
            return await self.fixture.storage.finalize_mcp_dispatch(
                self.authority.intent_id,
                self.authority.outbox_id,
                "node-1",
                "failed",
                "test_failure",
                current.revision,
                "worker",
                "token",
                self.now,
            )

        finalized = await self.coordinator._claim_update(self.authority, finalize)
        self.assertEqual(str(finalized), "finalized")
        self.assertFalse(self.authority.active)
        before = self.authority.revision
        await self.coordinator._renew_dispatch_claim(self.authority)
        self.assertFalse(self.authority.lost)
        self.assertEqual(self.authority.revision, before)

    async def test_revision_mutation_and_heartbeat_are_serialized(self):
        await self.coordinator._renew_dispatch_claim(self.authority)

        async def consume(current):
            await asyncio.sleep(0)
            return await self.fixture.storage.consume_mcp_dispatch_selector_step(
                self.authority.outbox_id,
                "worker",
                "token",
                current.revision,
                self.now,
            )

        for _ in range(8):
            consumed, _ = await asyncio.gather(
                self.coordinator._claim_update(self.authority, consume),
                self.coordinator._renew_dispatch_claim(self.authority),
            )
            self.assertIsNotNone(consumed)
        outbox = await self.fixture.storage.get_mcp_dispatch_resume_outbox(
            self.fixture.outbox_id
        )
        self.assertEqual(outbox.selector_step_total, 8)
        self.assertEqual(outbox.revision, self.authority.revision)

    async def test_lock_wait_consumes_budget_and_stops_new_work(self):
        self.authority.deadline = monotonic() + 0.03
        async with self.authority.lock:
            with self.assertRaises(ExecutionOwnershipLost):
                await self.coordinator._renew_dispatch_claim(self.authority)
        self.assertTrue(self.authority.lost)
        with self.assertRaises(ExecutionOwnershipLost):
            await self.coordinator._claim_update(
                self.authority, lambda _: self.fail("must not execute")
            )

    async def test_committed_mutation_with_lost_readback_is_not_retried(self):
        original_get = self.fixture.storage.get_mcp_dispatch_resume_outbox
        reads = 0

        async def get(outbox_id):
            nonlocal reads
            reads += 1
            if reads == 2:
                raise TimeoutError("readback response lost")
            return await original_get(outbox_id)

        self.fixture.storage.get_mcp_dispatch_resume_outbox = get
        try:
            with self.assertRaises(ExecutionOwnershipLost):
                await self.coordinator._claim_update(
                    self.authority,
                    lambda current: (
                        self.fixture.storage.consume_mcp_dispatch_selector_step(
                            self.authority.outbox_id,
                            "worker",
                            "token",
                            current.revision,
                            self.now,
                        )
                    ),
                )
        finally:
            self.fixture.storage.get_mcp_dispatch_resume_outbox = original_get
        saved = await original_get(self.authority.outbox_id)
        self.assertEqual(saved.selector_step_total, 1)
        with self.assertRaises(ExecutionOwnershipLost):
            await self.coordinator._claim_update(
                self.authority, lambda _: self.fail("must not retry")
            )

    async def test_expired_persisted_timestamp_is_not_reset_at_response_receipt(self):
        outbox = await self.fixture.storage.get_mcp_dispatch_resume_outbox(
            self.fixture.outbox_id
        )
        self.now += timedelta(seconds=13)
        self.assertLessEqual(self.coordinator._claim_deadline(outbox), monotonic())
        with self.assertRaises(ExecutionOwnershipLost):
            await self.coordinator._renew_dispatch_claim(self.authority)
