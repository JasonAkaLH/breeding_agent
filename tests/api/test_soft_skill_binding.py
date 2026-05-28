from __future__ import annotations

from src.core.enums import RoutingMode
from tests.api.support import APITestCase, GENERIC_DATA_SKILL_ID


class SoftSkillBindingAPITest(APITestCase):
    async def test_external_direct_skill_capability_is_rejected_before_task_creation(self) -> None:
        response = await self.client.post(
            "/api/v1/conversations/chat-messages",
            json={
                "conversation_id": "conv-direct",
                "content": "查询龙粳33",
                "routing_mode": "force_capability",
                "capability_id": GENERIC_DATA_SKILL_ID,
                "metadata": {"forced_by_slash_command": True, "slash_command": "/generic-data-lookup"},
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("direct_skill_execution_disabled", response.text)
        tasks = await self.runtime.storage.list_tasks_for_conversation("conv-direct")
        messages = await self.runtime.storage.list_messages_for_conversation("conv-direct")
        self.assertEqual(tasks, [])
        self.assertEqual(messages, [])

    async def test_external_direct_skill_is_rejected_even_when_routing_mode_auto(self) -> None:
        response = await self.client.post(
            "/api/v1/conversations/chat-messages",
            json={
                "conversation_id": "conv-direct-auto",
                "content": "查询龙粳33",
                "routing_mode": "auto",
                "capability_id": GENERIC_DATA_SKILL_ID,
                "metadata": {},
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("direct_skill_execution_disabled", response.text)

    async def test_soft_skill_binding_routes_initial_task_to_main_agent_and_executes_internal_skill(self) -> None:
        response = await self.client.post(
            "/api/v1/conversations/chat-messages",
            json={
                "conversation_id": "conv-soft",
                "content": "查询龙粳33",
                "routing_mode": "force_capability",
                "capability_id": "main_agent.respond",
                "metadata": {
                    "forced_by_slash_command": True,
                    "slash_command": "/generic-data-lookup",
                    "forced_skill_name": "malicious",
                    "soft_skill_binding": {
                        "capability_id": GENERIC_DATA_SKILL_ID,
                        "command": "/generic-data-lookup",
                    },
                },
            },
        )

        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]
        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")

        task = await self.runtime.storage.get_task(task_id)
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.routing_mode, RoutingMode.FORCE_CAPABILITY)
        self.assertEqual(task.requested_capability_id, "main_agent.respond")

        nodes = await self.runtime.storage.list_task_nodes_for_task(task_id)
        self.assertIn("main_agent.respond", [node.capability_id for node in nodes])
        self.assertIn(GENERIC_DATA_SKILL_ID, [node.capability_id for node in nodes])
        events = await self.runtime.storage.list_events_for_task(task_id)
        event_types = [event.event_type for event in events]
        self.assertIn("soft_skill_binding.decision", event_types)
        self.assertIn("skill.execution_completed", event_types)
        plan_event = next(event for event in events if event.event_type == "workflow.plan_built")
        self.assertEqual(plan_event.payload["metadata"]["skill_bundle_revision"], self.runtime._skill_runtime_state.active_revision)

    async def test_invalid_soft_skill_binding_is_rejected(self) -> None:
        response = await self.client.post(
            "/api/v1/conversations/chat-messages",
            json={
                "conversation_id": "conv-invalid-soft",
                "content": "执行",
                "routing_mode": "force_capability",
                "capability_id": "main_agent.respond",
                "metadata": {"soft_skill_binding": {"capability_id": "skill.unknown"}},
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("Unsupported soft_skill_binding capability_id", response.text)
