from __future__ import annotations

import unittest

from src.state.commands import build_command
from src.state.postgres.write_queue import IdempotencyConflictError
from src.state.postgres.write_queue import InMemoryPostgresWriteQueue
from src.state.service import InMemoryStateService


class StateServiceCommandGroupTest(unittest.IsolatedAsyncioTestCase):
    async def test_command_group_enqueues_atomically_or_rolls_back(self) -> None:
        queue = InMemoryPostgresWriteQueue()
        service = InMemoryStateService(queue=queue)
        first = build_command(command_type="conversation.create", idempotency_key="g1", payload={"conversation_id": "c"})
        second = build_command(command_type="conversation.create", idempotency_key="g2", payload={"conversation_id": "c"})
        records = await service.transactional_command_group([first, second])
        self.assertEqual(len(records), 2)
        bad = build_command(command_type="conversation.create", idempotency_key="g1", payload={"conversation_id": "c", "mismatch": True})
        with self.assertRaises(Exception):
            await service.transactional_command_group([bad])
        self.assertEqual(len(queue.records), 2)
    async def test_command_group_rollback_cleans_idempotency_and_partition_sequence(self) -> None:
        queue = InMemoryPostgresWriteQueue()
        service = InMemoryStateService(queue=queue)
        existing = build_command(command_type="conversation.create", idempotency_key="existing", payload={"conversation_id": "c"}, command_id="existing")
        await service.submit_command(existing)
        new_first = build_command(command_type="conversation.create", idempotency_key="new", payload={"conversation_id": "c"}, command_id="new")
        conflicting_second = build_command(
            command_type="conversation.create",
            idempotency_key="existing",
            payload={"conversation_id": "c", "mismatch": True},
            command_id="conflict",
        )
        with self.assertRaises(IdempotencyConflictError):
            await service.transactional_command_group([new_first, conflicting_second])
        self.assertEqual([record.command.command_id for record in queue.records], ["existing"])
        replay_new = await service.submit_command(new_first)
        self.assertEqual(replay_new.partition_sequence, 2)
