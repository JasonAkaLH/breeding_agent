from __future__ import annotations

from src.core.enums import NodeStatus

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

        async def task_running() -> bool:
            task = await self.runtime.storage.get_task(first_payload["task_id"])
            return task is not None and str(task.status) == "running"

        await self.wait_for_condition(task_running)
        cancel_response = await self.client.post(f"/api/v1/tasks/{first_payload['task_id']}/cancel")
        self.assertEqual(cancel_response.status_code, 202)
        release.set()
        terminal = await self.wait_for_terminal_task(first_payload["task_id"])
        self.assertEqual(terminal["status"], "cancelled")

    async def test_waiting_input_task_can_be_answered_and_resumed(self) -> None:
        first = await self.submit_message(content="帮我查询一下", capability_id="skill.generic_data_lookup")
        self.assertEqual(first.status_code, 202)
        first_payload = first.json()

        async def has_waiting_input_node() -> bool:
            nodes = await self.runtime.storage.list_task_nodes_for_task(first_payload["task_id"])
            return any(node.status == NodeStatus.WAITING_FOR_INPUT for node in nodes)

        await self.wait_for_condition(has_waiting_input_node)

        async def has_open_interrupt() -> bool:
            response = await self.client.get(f"/api/v1/tasks/{first_payload['task_id']}/interrupts")
            return response.status_code == 200 and any(
                interrupt["status"] == "open" for interrupt in response.json()["interrupts"]
            )

        await self.wait_for_condition(has_open_interrupt)

        interrupts = await self.client.get(f"/api/v1/tasks/{first_payload['task_id']}/interrupts")
        self.assertEqual(interrupts.status_code, 200)
        open_interrupt = interrupts.json()["interrupts"][0]
        self.assertEqual(open_interrupt["status"], "open")
        self.assertEqual(open_interrupt["reason_code"], "lookup_target_missing")

        answer = await self.client.post(
            f"/api/v1/tasks/{first_payload['task_id']}/interrupts/{open_interrupt['interrupt_id']}/answer",
            json={"answer_payload": {"lookup_target": "龙粳33"}},
        )
        self.assertEqual(answer.status_code, 202)
        self.assertEqual(answer.json()["status"], "answered")

        terminal = await self.wait_for_terminal_task(first_payload["task_id"])
        self.assertEqual(terminal["status"], "completed")

    async def test_legacy_generic_data_lookup_native_capability_id_is_rejected(self) -> None:
        response = await self.submit_message(content="查询龙粳33", capability_id="legacy.query")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Unsupported capability_id", response.text)
