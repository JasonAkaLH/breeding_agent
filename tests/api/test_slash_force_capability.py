from __future__ import annotations

from src.core.enums import RoutingMode
from tests.api.support import APITestCase, GENERIC_DATA_SKILL_ID


class SlashForceCapabilityAPITest(APITestCase):
    async def test_force_capability_requires_supported_capability_id(self) -> None:
        missing = await self.client.post(
            "/api/v1/conversations/conv-1/messages",
            json={
                "account_id": "acc-1",
                "content": "查询龙粳33",
                "routing_mode": "force_capability",
                "capability_id": None,
                "metadata": {"forced_by_slash_command": True, "slash_command": "/generic-data-lookup"},
            },
        )
        self.assertEqual(missing.status_code, 400)
        self.assertIn("capability_id is required", missing.text)

        unsupported = await self.client.post(
            "/api/v1/conversations/conv-1/messages",
            json={
                "account_id": "acc-1",
                "content": "查询龙粳33",
                "routing_mode": "force_capability",
                "capability_id": "skill.unknown",
                "metadata": {"forced_by_slash_command": True, "slash_command": "/unknown"},
            },
        )
        self.assertEqual(unsupported.status_code, 400)
        self.assertIn("Unsupported capability_id", unsupported.text)

    async def test_force_capability_stores_routing_mode_and_routes_to_skill(self) -> None:
        response = await self.client.post(
            "/api/v1/conversations/conv-1/messages",
            json={
                "account_id": "acc-1",
                "content": "查询龙粳33",
                "routing_mode": "force_capability",
                "capability_id": GENERIC_DATA_SKILL_ID,
                "metadata": {"forced_by_slash_command": True, "slash_command": "/generic-data-lookup"},
            },
        )
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]
        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")

        task = await self.runtime.storage.get_task(task_id)
        self.assertIsNotNone(task)
        self.assertEqual(task.routing_mode, RoutingMode.FORCE_CAPABILITY)
        self.assertEqual(task.requested_capability_id, GENERIC_DATA_SKILL_ID)

        nodes = await self.runtime.storage.list_task_nodes_for_task(task_id)
        self.assertCountEqual([node.capability_id for node in nodes], [GENERIC_DATA_SKILL_ID, "main_agent.respond"])
        events = await self.runtime.storage.list_events_for_task(task_id)
        self.assertIn("skill.execution_completed", [event.event_type for event in events])
