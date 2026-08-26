from __future__ import annotations

from unittest.mock import AsyncMock, patch

from src.core.enums import NodeStatus
from src.core.errors import MessageIdentityConflictError

from tests.api.support import (
    APITestCase,
    InMemoryTaskRuntimeSidecar,
    blocking_mysql_adapter,
)


class _IdentityRejectingSidecar(InMemoryTaskRuntimeSidecar):
    async def reserve_message_identity(self, **payload: object) -> dict[str, object]:
        self.calls.append(("message_identity_reserve", dict(payload)))
        raise AssertionError("disabled identity authority must not reserve")


class MessageSubmissionAPITest(APITestCase):
    async def test_enforce_submission_remains_accepted_while_a5_gate_is_disabled(
        self,
    ) -> None:
        sidecar = _IdentityRejectingSidecar()
        self.runtime.storage._mcp_task_authority_mode = "enforce"  # noqa: SLF001
        self.runtime.storage._runtime_sidecar_client = sidecar  # noqa: SLF001

        with patch.object(
            self.runtime,
            "_schedule_execution",
            new=AsyncMock(),
        ):
            response = await self.submit_message()

        self.assertEqual(response.status_code, 202, response.text)
        payload = response.json()
        self.assertIsNotNone(
            await self.runtime.storage.get_message(payload["message_id"])
        )
        self.assertNotIn(
            "message_identity_reserve",
            [operation for operation, _payload in sidecar.calls],
        )

    async def test_message_identity_conflict_returns_low_sensitive_conflict(self) -> None:
        conflict = MessageIdentityConflictError()
        conflict.existing_conversation_id = "private-conversation"
        conflict.existing_task_id = "private-task"

        with patch.object(
            self.runtime,
            "submit_chat_message",
            side_effect=conflict,
        ):
            response = await self.submit_message()

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json(),
            {"detail": {"code": "message_id_conflict"}},
        )

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
        cancel_response = await self.client.post("/api/v1/tasks/cancel", json={"task_id": first_payload['task_id']})
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
        events = await self.runtime.storage.list_events_for_task(first_payload["task_id"])
        waiting_events = [event for event in events if event.event_type == "node.waiting_for_input"]
        self.assertEqual(len(waiting_events), 1)
        self.assertEqual(waiting_events[0].node_id, open_interrupt["node_id"])
        self.assertEqual(waiting_events[0].payload["interrupt_id"], open_interrupt["interrupt_id"])
        self.assertEqual(waiting_events[0].payload["reason_code"], open_interrupt["reason_code"])

        answer = await self.client.post(
            "/api/v1/conversations/chat-messages",
            json={
                "conversation_id": first_payload["conversation_id"],
                "content": "龙粳33",
                "routing_mode": "auto",
                "capability_id": None,
                "client_message_id": "client-chat-interrupt-answer-legacy-test",
                "metadata": {"interrupt_id": open_interrupt["interrupt_id"]},
            },
        )
        self.assertEqual(answer.status_code, 202, answer.text)
        self.assertEqual(answer.json()["action"], "interrupt_resumed")

        terminal = await self.wait_for_terminal_task(first_payload["task_id"])
        self.assertEqual(terminal["status"], "completed")

    async def test_chat_message_answers_waiting_interrupt_without_creating_new_task(self) -> None:
        first = await self.submit_message(content="帮我查询一下", capability_id="skill.generic_data_lookup")
        self.assertEqual(first.status_code, 202)
        first_payload = first.json()

        async def has_open_interrupt() -> bool:
            response = await self.client.get(f"/api/v1/tasks/{first_payload['task_id']}/interrupts")
            return response.status_code == 200 and any(
                interrupt["status"] == "open" for interrupt in response.json()["interrupts"]
            )

        await self.wait_for_condition(has_open_interrupt)
        interrupts = await self.client.get(f"/api/v1/tasks/{first_payload['task_id']}/interrupts")
        open_interrupt = interrupts.json()["interrupts"][0]

        answer = await self.client.post(
            "/api/v1/conversations/chat-messages",
            json={
                "conversation_id": first_payload["conversation_id"],
                "content": "龙粳33",
                "routing_mode": "auto",
                "capability_id": None,
                "client_message_id": "client-chat-interrupt-answer-1",
                "metadata": {"interrupt_id": open_interrupt["interrupt_id"]},
            },
        )
        self.assertEqual(answer.status_code, 202, answer.text)
        payload = answer.json()
        self.assertEqual(payload["conversation_id"], first_payload["conversation_id"])
        self.assertEqual(payload["task_id"], first_payload["task_id"])
        self.assertEqual(payload["message_id"], "client-chat-interrupt-answer-1")
        self.assertEqual(payload["action"], "interrupt_resumed")
        self.assertEqual(payload["interrupt_id"], open_interrupt["interrupt_id"])

        terminal = await self.wait_for_terminal_task(first_payload["task_id"])
        self.assertEqual(terminal["status"], "completed")
        conversations = await self.runtime.storage.list_tasks_for_conversation(first_payload["conversation_id"])
        self.assertEqual([task.task_id for task in conversations], [first_payload["task_id"]])

    async def test_chat_message_with_stale_interrupt_id_does_not_create_new_task(self) -> None:
        first = await self.submit_message(content="你好")
        self.assertEqual(first.status_code, 202)
        first_payload = first.json()
        terminal = await self.wait_for_terminal_task(first_payload["task_id"])
        self.assertEqual(terminal["status"], "completed")

        stale = await self.client.post(
            "/api/v1/conversations/chat-messages",
            json={
                "conversation_id": first_payload["conversation_id"],
                "content": "这本来想回答一个旧 interrupt",
                "routing_mode": "auto",
                "capability_id": None,
                "metadata": {"interrupt_id": "interrupt-stale"},
            },
        )
        self.assertEqual(stale.status_code, 400)
        self.assertIn("No active task is waiting for interrupt", stale.text)
        tasks = await self.runtime.storage.list_tasks_for_conversation(first_payload["conversation_id"])
        self.assertEqual([task.task_id for task in tasks], [first_payload["task_id"]])

    async def test_legacy_generic_data_lookup_native_capability_id_is_rejected(self) -> None:
        response = await self.submit_message(content="查询龙粳33", capability_id="legacy.query")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Unsupported capability_id", response.text)
