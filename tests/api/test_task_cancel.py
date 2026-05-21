from __future__ import annotations

import asyncio

from httpx_sse import aconnect_sse

from src.core.enums import TaskStatus
from src.core.models import Conversation, Task

from tests.api.support import APITestCase, blocking_mysql_adapter


class TaskCancelAPITest(APITestCase):
    async def test_cancel_endpoint_drives_real_cancellation_and_audit_output(self) -> None:
        blocking_adapter, release = blocking_mysql_adapter()
        await self.reconfigure_runtime(mysql_adapter=blocking_adapter)

        response = await self.submit_message()
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]

        async def _task_started() -> bool:
            current = await self.runtime.storage.get_task(task_id)
            return current is not None and current.status == "running"

        await self.wait_for_condition(_task_started)

        cancel_response = await self.client.post("/api/v1/tasks/cancel", json={"task_id": task_id})
        self.assertEqual(cancel_response.status_code, 202)
        self.assertTrue(cancel_response.json()["accepted"])

        async with aconnect_sse(self.client, "GET", f"/api/v1/tasks/{task_id}/events") as event_source:
            iterator = event_source.aiter_sse()
            seen = set()
            while "task.cancelled" not in seen:
                event = await asyncio.wait_for(iterator.__anext__(), timeout=2)
                seen.add(event.event)

        release.set()
        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "cancelled")
        self.assertTrue(terminal["cancel_requested"])

        audit_log = (self.workspace / "audit.jsonl").read_text(encoding="utf-8")
        self.assertIn("task.context_terminated", audit_log)
        self.assertIn("task.cancelled", audit_log)

    async def test_cancel_terminal_task_is_idempotent_and_does_not_rewrite_status(self) -> None:
        await self.runtime.storage.save_conversation(Conversation(conversation_id="conv-1", account_id="acc-1"))
        task = Task(
            task_id="task-terminal-api",
            conversation_id="conv-1",
            root_message_id="msg-terminal-api",
            status=TaskStatus.COMPLETED,
        )
        await self.runtime.storage.save_task(task)

        cancel_response = await self.client.post("/api/v1/tasks/cancel", json={"task_id": "task-terminal-api"})

        self.assertEqual(cancel_response.status_code, 202)
        self.assertEqual(cancel_response.json()["status"], "completed")
        reloaded = await self.runtime.storage.get_task("task-terminal-api")
        self.assertEqual(reloaded.status, TaskStatus.COMPLETED)
        self.assertIsNone(reloaded.cancel_requested_at)
