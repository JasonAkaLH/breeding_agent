from __future__ import annotations

import _bootstrap  # noqa: F401

import asyncio

from support import SQLQueryE2EAPITestCase


class SQLQueryHappyPathE2ETest(SQLQueryE2EAPITestCase):
    async def test_happy_path_runs_from_message_to_table_and_sse_completion(self) -> None:
        response = await self.submit_message(content="查询品种龙粳33的基因型信息")
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]

        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")
        self.assertEqual(terminal["completed_node_count"], 2)

        iterator = self.runtime.iter_frontend_events(task_id).__aiter__()
        seen = set()
        while "task.completed" not in seen:
            event = await asyncio.wait_for(iterator.__anext__(), timeout=2)
            seen.add(event.event_type)

        self.assertIn("task.accepted", seen)
        self.assertIn("task.graph_created", seen)
        self.assertIn("node.started", seen)
        self.assertIn("task.completed", seen)

        artifacts_response = await self.client.get(f"/api/v1/tasks/{task_id}/artifacts")
        self.assertEqual(artifacts_response.status_code, 200)
        artifacts = artifacts_response.json()["artifacts"]
        self.assertTrue(any("query_result_preview" in artifact["artifact_id"] for artifact in artifacts))
        self.assertTrue(any("filtered_query_result" in artifact["artifact_id"] for artifact in artifacts))
