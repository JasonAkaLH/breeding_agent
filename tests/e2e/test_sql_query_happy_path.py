from __future__ import annotations

import asyncio

from tests.e2e.support import E2EAPITestCase


class SQLQueryHappyPathE2ETest(E2EAPITestCase):
    async def test_happy_path_runs_from_message_to_summary_and_sse_completion(self) -> None:
        await self.reconfigure_runtime(
            summarizer=lambda payload: f"验收摘要: {payload['row_count']} 行",
        )

        response = await self.submit_message(content="查询品种龙粳33的基因型信息")
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]

        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")
        self.assertEqual(terminal["completed_node_count"], 6)

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
        self.assertTrue(any("result_summary" in artifact["artifact_id"] for artifact in artifacts))
        self.assertTrue(any("验收摘要" in (artifact["summary"] or "") for artifact in artifacts))
