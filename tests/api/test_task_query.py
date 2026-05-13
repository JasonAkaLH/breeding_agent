from __future__ import annotations

from tests.api.support import APITestCase


class TaskQueryAPITest(APITestCase):
    async def test_task_query_graph_and_artifacts_endpoints_return_expected_shape(self) -> None:
        response = await self.submit_message()
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]

        task_payload = await self.wait_for_terminal_task(task_id)
        self.assertEqual(task_payload["status"], "completed")
        self.assertIsNotNone(task_payload["root_node_id"])
        self.assertEqual(task_payload["completed_node_count"], 2)
        self.assertEqual(task_payload["failed_node_count"], 0)
        self.assertFalse(task_payload["cancel_requested"])
        self.assertEqual(task_payload["conversation_id"], "conv-1")

        graph_response = await self.client.get(f"/api/v1/tasks/{task_id}/graph")
        self.assertEqual(graph_response.status_code, 200)
        graph_payload = graph_response.json()
        self.assertEqual(graph_payload["task_id"], task_id)
        self.assertEqual(len(graph_payload["nodes"]), 2)
        self.assertEqual(len(graph_payload["edges"]), 1)
        self.assertEqual({node["capability_id"] for node in graph_payload["nodes"]}, {"skill.generic_data_lookup", "main_agent.respond"})

        artifacts_response = await self.client.get(f"/api/v1/tasks/{task_id}/artifacts")
        self.assertEqual(artifacts_response.status_code, 200)
        artifacts_payload = artifacts_response.json()
        self.assertEqual(artifacts_payload["task_id"], task_id)
        self.assertGreaterEqual(len(artifacts_payload["artifacts"]), 1)
