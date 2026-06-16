from __future__ import annotations

import json

from src.integrations.mysql_readonly import MySQLReadonlyAdapter, ReadonlyQueryResult
from tests.api.support import APITestCase


class RuntimeReplannerAPITest(APITestCase):
    async def test_default_runtime_replanner_keeps_legacyquery_as_single_skill_node(self) -> None:
        def planner(_prompt: str) -> str:
            return json.dumps({"nodes": [{"node_id": "query_data", "capability_id": "skill.generic_data_lookup"}]})

        await self.reconfigure_runtime(
            mysql_adapter=MySQLReadonlyAdapter(
                runner=lambda _sql: ReadonlyQueryResult(columns=("variety_name",), rows=({"variety_name": "龙粳33"},), row_count=1)
            ),
            planner_text_generator=planner,
            main_agent_stream_generator=lambda _prompt, **_kwargs: "汇总回答",
            enable_platform_llm=False,
            skill_roots=None,
        )

        response = await self.submit_message(content="查询龙粳33", capability_id=None)
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]
        terminal = await self.wait_for_terminal_task(task_id)
        graph_response = await self.client.get(f"/api/v1/tasks/{task_id}/graph")
        graph_response.raise_for_status()
        graph = graph_response.json()

        self.assertEqual(terminal["status"], "completed")
        self.assertEqual({node["capability_id"] for node in graph["nodes"]}, {"skill.generic_data_lookup", "main_agent.respond"})
        self.assertFalse(any("runtime_query_1" in node["node_id"] for node in graph["nodes"]))


if __name__ == "__main__":
    import unittest

    unittest.main()
