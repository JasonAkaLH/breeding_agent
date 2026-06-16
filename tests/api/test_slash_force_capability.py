from __future__ import annotations

from src.core.enums import RoutingMode
from tests.api.support import APITestCase, GENERIC_DATA_SKILL_ID


class SlashForceCapabilityAPITest(APITestCase):
    async def test_force_capability_requires_supported_non_skill_capability_id(self) -> None:
        missing = await self.client.post(
            "/api/v1/conversations/chat-messages",
            json={
                "conversation_id": "conv-1",
                "content": "查询龙粳33",
                "routing_mode": "force_capability",
                "capability_id": None,
                "metadata": {"forced_by_slash_command": True, "slash_command": "/generic-data-lookup"},
            },
        )
        self.assertEqual(missing.status_code, 400)
        self.assertIn("capability_id is required", missing.text)

        direct_skill = await self.client.post(
            "/api/v1/conversations/chat-messages",
            json={
                "conversation_id": "conv-1",
                "content": "查询龙粳33",
                "routing_mode": "force_capability",
                "capability_id": GENERIC_DATA_SKILL_ID,
                "metadata": {"forced_by_slash_command": True, "slash_command": "/unknown"},
            },
        )
        self.assertEqual(direct_skill.status_code, 400)
        self.assertIn("direct_skill_execution_disabled", direct_skill.text)

    async def test_force_capability_with_soft_binding_stores_main_agent_route_and_runs_skill_internally(self) -> None:
        response = await self.client.post(
            "/api/v1/conversations/chat-messages",
            json={
                "conversation_id": "conv-1",
                "content": "查询龙粳33",
                "routing_mode": "force_capability",
                "capability_id": "main_agent.respond",
                "metadata": {
                    "forced_by_slash_command": True,
                    "slash_command": "/generic-data-lookup",
                    "soft_skill_binding": {
                        "capability_id": GENERIC_DATA_SKILL_ID,
                        "command": "/generic-data-lookup",
                    },
                },
            },
        )
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]
        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")

        task = await self.runtime.storage.get_task(task_id)
        self.assertIsNotNone(task)
        self.assertEqual(task.routing_mode, RoutingMode.FORCE_CAPABILITY)
        self.assertEqual(task.requested_capability_id, "main_agent.respond")

        nodes = await self.runtime.storage.list_task_nodes_for_task(task_id)
        self.assertIn(GENERIC_DATA_SKILL_ID, [node.capability_id for node in nodes])
        self.assertIn("main_agent.respond", [node.capability_id for node in nodes])
        events = await self.runtime.storage.list_events_for_task(task_id)
        self.assertIn("soft_skill_binding.decision", [event.event_type for event in events])
        self.assertIn("skill.execution_completed", [event.event_type for event in events])
