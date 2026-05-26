from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from src.state.commands import build_command
from src.state.contracts import CommandStatus
from src.state.errors import StatePlatformError
from src.state.postgres.write_queue import IdempotencyConflictError, InMemoryPostgresWriteQueue


class PostgresWriteQueueContractTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.now = datetime(2026, 5, 26, tzinfo=timezone.utc).replace(tzinfo=None)
        self.queue = InMemoryPostgresWriteQueue(now_fn=lambda: self.now)

    async def test_enqueue_is_idempotent_for_same_payload_and_rejects_mismatch(self) -> None:
        command = build_command(
            command_type="conversation.create",
            idempotency_key="idem-1",
            payload={"conversation_id": "c1", "title": "A"},
            command_id="cmd-1",
            created_at=self.now,
        )
        first = await self.queue.enqueue(command)
        duplicate = await self.queue.enqueue(command)
        self.assertEqual(first.command.command_id, duplicate.command.command_id)
        mismatched = build_command(
            command_type="conversation.create",
            idempotency_key="idem-1",
            payload={"conversation_id": "c1", "title": "B"},
            command_id="cmd-2",
            created_at=self.now,
        )
        with self.assertRaises(IdempotencyConflictError):
            await self.queue.enqueue(mismatched)

    async def test_claim_respects_same_partition_order_and_cross_partition_parallelism(self) -> None:
        c1 = build_command(command_type="message.append", idempotency_key="a1", payload={"conversation_id": "c1"}, command_id="cmd-a1", created_at=self.now)
        c2 = build_command(command_type="message.append", idempotency_key="a2", payload={"conversation_id": "c1"}, command_id="cmd-a2", created_at=self.now)
        b1 = build_command(command_type="message.append", idempotency_key="b1", payload={"conversation_id": "c2"}, command_id="cmd-b1", created_at=self.now)
        await self.queue.enqueue(c1)
        await self.queue.enqueue(c2)
        await self.queue.enqueue(b1)
        first = await self.queue.claim_next(worker_id="w1", lease_seconds=30)
        second = await self.queue.claim_next(worker_id="w2", lease_seconds=30)
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual({first.command.command_id, second.command.command_id}, {"cmd-a1", "cmd-b1"})  # type: ignore[union-attr]
        self.assertIsNone(await self.queue.claim_next(worker_id="w3", lease_seconds=30))
        await self.queue.complete("cmd-a1", {"ok": True})
        third = await self.queue.claim_next(worker_id="w3", lease_seconds=30)
        self.assertIsNotNone(third)
        self.assertEqual(third.command.command_id, "cmd-a2")  # type: ignore[union-attr]

    async def test_lease_expiry_allows_reclaim(self) -> None:
        command = build_command(command_type="task.update", idempotency_key="t1", payload={"task_id": "t1"}, command_id="cmd-t1", created_at=self.now)
        await self.queue.enqueue(command)
        first = await self.queue.claim_next(worker_id="w1", lease_seconds=5)
        self.assertIsNotNone(first)
        self.assertEqual(first.status, CommandStatus.LEASED)  # type: ignore[union-attr]
        self.assertIsNone(await self.queue.claim_next(worker_id="w2", lease_seconds=5))
        self.now = self.now + timedelta(seconds=6)
        reclaimed = await self.queue.claim_next(worker_id="w2", lease_seconds=5)
        self.assertIsNotNone(reclaimed)
        self.assertEqual(reclaimed.command.command_id, "cmd-t1")  # type: ignore[union-attr]
        self.assertEqual(reclaimed.lease_owner, "w2")  # type: ignore[union-attr]

    async def test_retry_exhaustion_dead_letters_with_redacted_error(self) -> None:
        command = build_command(command_type="task.update", idempotency_key="t2", payload={"task_id": "t2"}, command_id="cmd-t2", created_at=self.now)
        await self.queue.enqueue(command, max_attempts=2)
        await self.queue.claim_next(worker_id="w1", lease_seconds=5)
        retry = await self.queue.fail("cmd-t2", StatePlatformError("postgres_deadlock", "deadlock", retryable=True, sqlstate="40P01"))
        self.assertEqual(retry.status, CommandStatus.RETRY_SCHEDULED)
        self.now = self.now + timedelta(seconds=61)
        await self.queue.claim_next(worker_id="w1", lease_seconds=5)
        dead = await self.queue.fail("cmd-t2", StatePlatformError("postgres_deadlock", "dsn=<fixture-dsn> token=<fixture>", retryable=True, sqlstate="40P01"))
        self.assertEqual(dead.status, CommandStatus.DEAD_LETTERED)
        self.assertEqual(len(self.queue.dead_letters), 1)
        self.assertNotIn("postgresql://", repr(self.queue.dead_letters[0].public_dict()))
        self.assertNotIn("abc", repr(self.queue.dead_letters[0].public_dict()))

    async def test_retry_requires_allowlisted_sqlstate_even_when_error_claims_retryable(self) -> None:
        command = build_command(command_type="task.update", idempotency_key="t3", payload={"task_id": "t3"}, command_id="cmd-t3", created_at=self.now)
        await self.queue.enqueue(command, max_attempts=3)
        await self.queue.claim_next(worker_id="w1", lease_seconds=5)
        failed = await self.queue.fail("cmd-t3", StatePlatformError("business_error", "business", retryable=True, sqlstate="P0001"))
        self.assertEqual(failed.status, CommandStatus.FAILED)
