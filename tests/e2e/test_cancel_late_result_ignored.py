from __future__ import annotations

from tests.api.support import GENERIC_DATA_SKILL_ID, blocking_mysql_adapter
from tests.e2e.support import E2EAPITestCase


class CancelLateResultE2ETest(E2EAPITestCase):
    async def test_cancelled_task_does_not_accept_released_result(self) -> None:
        blocking_adapter, release = blocking_mysql_adapter()
        await self.reconfigure_runtime(mysql_adapter=blocking_adapter)

        response = await self.submit_message(content="查询品种龙粳33的基因型信息")
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]

        async def skill_is_running() -> bool:
            nodes = await self.runtime.storage.list_task_nodes_for_task(task_id)
            return any(
                node.capability_id == GENERIC_DATA_SKILL_ID
                and str(node.status) == "running"
                for node in nodes
            )

        await self.wait_for_condition(skill_is_running)

        cancel_response = await self.client.post("/api/v1/tasks/cancel", json={"task_id": task_id})
        self.assertEqual(cancel_response.status_code, 202)

        release.set()
        await self.runtime._await_existing_execution(task_id)
        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "cancelled")

        nodes = await self.runtime.storage.list_task_nodes_for_task(task_id)
        skill_node = next(
            node for node in nodes if node.capability_id == GENERIC_DATA_SKILL_ID
        )
        self.assertEqual(str(skill_node.status), "cancelled")
        events = await self.runtime.storage.list_events_for_task(task_id)
        self.assertNotIn("skill.execution_completed", {event.event_type for event in events})
