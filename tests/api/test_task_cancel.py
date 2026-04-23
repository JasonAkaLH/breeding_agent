from __future__ import annotations

import asyncio

from httpx_sse import aconnect_sse

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

        cancel_response = await self.client.post(f"/api/v1/tasks/{task_id}/cancel")
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
