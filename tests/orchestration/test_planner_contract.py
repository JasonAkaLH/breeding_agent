from __future__ import annotations

import unittest

from src.orchestration.models import CapabilityDescriptor, OrchestrationRequest
from src.orchestration.planner_contract import (
    PUBLIC_CAPABILITY_LIST_BUDGET_CHARS,
    PlannerOutputError,
    build_plan_from_llm_output,
    build_planner_prompt,
    parse_planner_output,
)


class PlannerContractTest(unittest.IsolatedAsyncioTestCase):
    async def test_fake_llm_output_builds_high_level_public_dag(self) -> None:
        async def fake_generator(prompt: str) -> str:
            self.assertIn("skill.generic_data_lookup", prompt)
            return """
            {
              "nodes": [
                {"node_id": "query_data", "capability_id": "skill.generic_data_lookup", "input_payload": {"user_question": "查询龙粳33"}},
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

        plan = await build_plan_from_llm_output(
            request,
            text_generator=fake_generator,
            public_capabilities=(
                CapabilityDescriptor(
                    capability_id="skill.generic_data_lookup",
                    name="generic-data-lookup",
                    description="安全回答数据库类只读查询问题。",
                    kind="skill",
                    source="skill",
                ),
            ),
        )

        self.assertEqual(plan.task_id, "task-1")
        self.assertEqual([node.node_id for node in plan.nodes], ["query_data", "answer_user"])
        self.assertEqual(plan.nodes[0].capability_id, "skill.generic_data_lookup")
        self.assertEqual(plan.nodes[1].depends_on, ("query_data",))
        self.assertEqual(plan.metadata["source"], "llm_planner_output")

    def test_parse_rejects_missing_nodes_array(self) -> None:
        with self.assertRaisesRegex(PlannerOutputError, "nodes"):
            parse_planner_output('{"plan": []}', task_id="task-1")

    def test_parse_accepts_json_wrapped_in_markdown_code_fence(self) -> None:
        plan = parse_planner_output(
            """
            ```json
            {
              "nodes": [
                {"node_id": "query_data", "capability_id": "skill.generic_data_lookup"}
              ]
            }
            ```
            """,
            task_id="task-1",
        )

        self.assertEqual(plan.nodes[0].node_id, "query_data")
        self.assertEqual(plan.nodes[0].capability_id, "skill.generic_data_lookup")

    def test_parse_rejects_non_object_node(self) -> None:
        with self.assertRaisesRegex(PlannerOutputError, "node object"):
            parse_planner_output('{"nodes": ["bad"]}', task_id="task-1")

    def test_planner_prompt_includes_skill_path_but_not_skill_body(self) -> None:
        prompt = build_planner_prompt(
            OrchestrationRequest(
                task_id="task-skill",
                conversation_id="conv-1",
                root_message_id="msg-1",
                user_message="处理材料表",
            ),
            public_capabilities=(
                CapabilityDescriptor(
                    capability_id="skill.demo",
                    name="demo",
                    description="处理演示任务",
                    kind="skill",
                    source="skill",
                    source_path="demo/SKILL.md",
                ),
            ),
        )

        self.assertIn("skill.demo", prompt)
        self.assertIn("路径：demo/SKILL.md", prompt)
        self.assertNotIn("完整 Skill 指令", prompt)

    def test_public_capability_block_is_budgeted_by_shortening_and_omitting_entries(self) -> None:
        long_description = "能力描述" + ("x" * 500)
        capabilities = tuple(
            CapabilityDescriptor(
                capability_id=f"skill.demo_{index}",
                name=f"demo-{index}",
                description=long_description,
                kind="skill",
                source="skill",
                source_path=f"demo-{index}/SKILL.md",
            )
            for index in range(120)
        )

        prompt = build_planner_prompt(
            OrchestrationRequest(
                task_id="task-budget",
                conversation_id="conv-1",
                root_message_id="msg-1",
                user_message="处理材料表",
            ),
            public_capabilities=capabilities,
        )
        capability_block = prompt.split("可用 public capability：\n", 1)[1].split("\n\n当前用户问题区块：", 1)[0]

        self.assertLessEqual(len(capability_block), PUBLIC_CAPABILITY_LIST_BUDGET_CHARS)
        self.assertIn("skill.demo_0", capability_block)
        self.assertIn("部分 capability 因列表预算被省略", capability_block)
        self.assertNotIn("x" * 300, capability_block)


if __name__ == "__main__":
    unittest.main()
