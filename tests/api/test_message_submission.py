from __future__ import annotations

from tests.api.support import APITestCase, blocking_mysql_adapter


class MessageSubmissionAPITest(APITestCase):
    async def test_message_submission_returns_accepted_and_rejects_busy_conversation(self) -> None:
        blocking_adapter, release = blocking_mysql_adapter()
        await self.reconfigure_runtime(mysql_adapter=blocking_adapter)

        first = await self.submit_message()
        self.assertEqual(first.status_code, 202)
        first_payload = first.json()
        self.assertEqual(first_payload["conversation_id"], "conv-1")
        self.assertEqual(first_payload["status"], "accepted")
        self.assertIn("task_id", first_payload)
        self.assertIn("message_id", first_payload)

        second = await self.submit_message(content="再来一条消息")
        self.assertEqual(second.status_code, 409)

        cancel_response = await self.client.post(f"/api/v1/tasks/{first_payload['task_id']}/cancel")
        self.assertEqual(cancel_response.status_code, 202)
        release.set()
        terminal = await self.wait_for_terminal_task(first_payload["task_id"])
        self.assertEqual(terminal["status"], "cancelled")
