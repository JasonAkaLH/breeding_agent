from __future__ import annotations

import threading

from tests.api.support import APITestCase, blocking_mysql_adapter


class TaskListAPITest(APITestCase):
    async def test_missing_conversation_task_list_returns_empty_for_documented_recovery_flow(self) -> None:
        response = await self.client.get("/api/v1/conversations/missing-conversation/tasks?scope=all")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"conversation_id": "missing-conversation", "tasks": []},
        )

    async def test_conversation_unfinished_task_list_can_drive_stop_action(self) -> None:
        query_started = threading.Event()
        blocking_adapter, release = blocking_mysql_adapter(started=query_started)
        await self.reconfigure_runtime(mysql_adapter=blocking_adapter)

        response = await self.submit_message(content="查询龙粳33", capability_id="skill.generic_data_lookup")
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]

        async def task_running_with_active_node() -> bool:
            task = await self.runtime.storage.get_task(task_id)
            nodes = await self.runtime.storage.list_task_nodes_for_task(task_id)
            return (
                task is not None
                and str(task.status) == "running"
                and query_started.is_set()
                and any(str(node.status) in {"running", "ready"} for node in nodes)
            )

        await self.wait_for_condition(task_running_with_active_node)

        list_response = await self.client.get("/api/v1/conversations/conv-1/tasks?scope=unfinished")
        self.assertEqual(list_response.status_code, 200)
        listed = list_response.json()["tasks"]
        self.assertEqual([task["task_id"] for task in listed], [task_id])
        self.assertEqual(listed[0]["summary"], "查询龙粳33")
        self.assertEqual(listed[0]["requested_capability_id"], "main_agent.respond")
        self.assertGreaterEqual(listed[0]["active_node_count"], 1)

        cancel_response = await self.client.post("/api/v1/tasks/cancel", json={"task_id": task_id})
        self.assertEqual(cancel_response.status_code, 202)

        async def unfinished_list_empty() -> bool:
            after_cancel = await self.client.get("/api/v1/conversations/conv-1/tasks?scope=unfinished")
            self.assertEqual(after_cancel.status_code, 200)
            return after_cancel.json()["tasks"] == []

        try:
            await self.wait_for_condition(unfinished_list_empty)
        finally:
            release.set()
