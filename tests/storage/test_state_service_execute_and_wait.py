from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timezone

from src.state.commands import build_command
from src.state.contracts import CommandStatus, StateCommandResult
from src.state.postgres.write_queue import InMemoryPostgresWriteQueue
from src.state.service import InMemoryStateService


class StateServiceExecuteAndWaitTest(unittest.IsolatedAsyncioTestCase):
    async def test_execute_and_wait_returns_success_for_completed_command(self) -> None:
        queue = InMemoryPostgresWriteQueue(now_fn=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
        service = InMemoryStateService(queue=queue)
        command = build_command(command_type="conversation.create", idempotency_key="c", payload={"conversation_id": "c"})

        async def worker() -> None:
            await asyncio.sleep(0.01)
            claimed = await queue.claim_next(worker_id="worker")
            self.assertIsNotNone(claimed)
            await queue.complete(claimed.command.command_id, {"ok": True})  # type: ignore[union-attr]

        task = asyncio.create_task(worker())
        result = await service.execute_command_and_wait(command, timeout_seconds=1)
        await task
        self.assertEqual(result.status, CommandStatus.SUCCEEDED)
        self.assertTrue(result.result["ok"])

    async def test_execute_and_wait_timeout_does_not_fake_success(self) -> None:
        service = InMemoryStateService(queue=InMemoryPostgresWriteQueue())
        command = build_command(command_type="conversation.create", idempotency_key="timeout", payload={"conversation_id": "c"})
        result = await service.execute_command_and_wait(command, timeout_seconds=0.01)
        self.assertEqual(result.status, CommandStatus.FAILED)
        self.assertIsNotNone(result.error)
        self.assertEqual(result.error.code, "state_command_timeout")  # type: ignore[union-attr]

    async def test_idempotent_replay_returns_existing_terminal_result(self) -> None:
        queue = InMemoryPostgresWriteQueue()
        service = InMemoryStateService(queue=queue)
        command = build_command(command_type="conversation.create", idempotency_key="replay", payload={"conversation_id": "c"})
        record = await service.submit_command(command)
        await queue.claim_next(worker_id="worker")
        await queue.complete(record.command.command_id, {"replayed": True})
        result = await service.execute_command_and_wait(command, timeout_seconds=0.1)
        self.assertEqual(result, StateCommandResult(record.command.command_id, CommandStatus.SUCCEEDED, {"replayed": True}, completed_at=result.completed_at))
