from __future__ import annotations

import asyncio

from src.core.enums import TaskStatus
from src.core.models import Task
from src.lifecycle.conversation_guard import ConversationSerialGuard
from src.lifecycle.errors import ConversationBusyError
from tests.lifecycle.support import LifecycleSQLiteTestCase


class ConversationSerialGuardTest(LifecycleSQLiteTestCase):
    def test_active_task_blocks_new_task_in_same_conversation(self) -> None:
        guard = ConversationSerialGuard(self.storage)
        task = Task(task_id="task-1", conversation_id="conv-1", root_message_id="msg-1", status=TaskStatus.RUNNING)
        asyncio.run(self.storage.save_task(task))

        with self.assertRaises(ConversationBusyError):
            asyncio.run(guard.ensure_conversation_available("conv-1"))

    def test_completed_task_does_not_block_new_task(self) -> None:
        guard = ConversationSerialGuard(self.storage)
        task = Task(task_id="task-1", conversation_id="conv-1", root_message_id="msg-1", status=TaskStatus.COMPLETED)
        asyncio.run(self.storage.save_task(task))

        available = asyncio.run(guard.ensure_conversation_available("conv-1"))
        self.assertIsNone(available)
