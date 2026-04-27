from __future__ import annotations

import unittest

from src.orchestration.models import OrchestrationRequest
from src.orchestration.planner_contract import PlannerOutputError, build_plan_from_llm_output, parse_planner_output


class PlannerContractTest(unittest.IsolatedAsyncioTestCase):
    async def test_fake_llm_output_builds_high_level_public_dag(self) -> None:
        async def fake_generator(prompt: str) -> str:
            self.assertIn("sql_query.query", prompt)
            return """
            {
              "nodes": [
                {"node_id": "query_data", "capability_id": "sql_query.query", "input_payload": {"user_question": "查询龙粳33"}},
                {"node_id": "answer_user", "capability_id": "main_agent.respond", "depends_on": ["query_data"]}
              ]
            }
            """

        request = OrchestrationRequest(
            task_id="task-1",
            conversation_id="conv-1",
            root_message_id="msg-1",
            user_message="查询龙粳33并回答用户",
        )

        plan = await build_plan_from_llm_output(request, text_generator=fake_generator)

        self.assertEqual(plan.task_id, "task-1")
        self.assertEqual([node.node_id for node in plan.nodes], ["query_data", "answer_user"])
        self.assertEqual(plan.nodes[0].capability_id, "sql_query.query")
        self.assertEqual(plan.nodes[1].depends_on, ("query_data",))
        self.assertEqual(plan.metadata["source"], "llm_planner_output")

    def test_parse_rejects_missing_nodes_array(self) -> None:
        with self.assertRaisesRegex(PlannerOutputError, "nodes"):
            parse_planner_output('{"plan": []}', task_id="task-1")

    def test_parse_rejects_non_object_node(self) -> None:
        with self.assertRaisesRegex(PlannerOutputError, "node object"):
            parse_planner_output('{"nodes": ["bad"]}', task_id="task-1")


if __name__ == "__main__":
    unittest.main()
