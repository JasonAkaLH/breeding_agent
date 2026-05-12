from __future__ import annotations

from tests.api.support import APITestCase, blocking_mysql_adapter


class TaskListAPITest(APITestCase):
    async def test_conversation_unfinished_task_list_can_drive_stop_action(self) -> None:
        blocking_adapter, release = blocking_mysql_adapter()
        await self.reconfigure_runtime(mysql_adapter=blocking_adapter)

        response = await self.submit_message(content="查询龙粳33", capability_id="sql_query.query")
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]

        async def task_running() -> bool:
            task = await self.runtime.storage.get_task(task_id)
            return task is not None and str(task.status) == "running"

        await self.wait_for_condition(task_running)

        list_response = await self.client.get("/api/v1/conversations/conv-1/tasks?scope=unfinished")
        self.assertEqual(list_response.status_code, 200)
        listed = list_response.json()["tasks"]
        self.assertEqual([task["task_id"] for task in listed], [task_id])
        self.assertEqual(listed[0]["summary"], "查询龙粳33")
        self.assertEqual(listed[0]["requested_capability_id"], "skill.sql_query")
        self.assertGreaterEqual(listed[0]["active_node_count"], 1)

        cancel_response = await self.client.post(f"/api/v1/tasks/{task_id}/cancel")
        self.assertEqual(cancel_response.status_code, 202)

        after_cancel = await self.client.get("/api/v1/conversations/conv-1/tasks?scope=unfinished")
        self.assertEqual(after_cancel.status_code, 200)
        self.assertEqual(after_cancel.json()["tasks"], [])

        release.set()
