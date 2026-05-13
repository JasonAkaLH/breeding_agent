from __future__ import annotations

import unittest

from src.capabilities.main_agent import MainAgentWorkflowProvider
from src.orchestration.auto_workflow_provider import AutoWorkflowProvider
from src.orchestration.models import OrchestrationRequest


class AutoWorkflowProviderTest(unittest.TestCase):
    def test_database_question_falls_back_to_main_agent_without_business_hardcode(self) -> None:
        provider = AutoWorkflowProvider(main_agent_provider=MainAgentWorkflowProvider())

        plan = provider.build_plan(
            OrchestrationRequest(
                task_id="task-1",
                conversation_id="conv-1",
                root_message_id="msg-1",
                user_message="查询龙粳18的详细审定信息",
            )
        )

        self.assertEqual([node.capability_id for node in plan.nodes], ["main_agent.respond"])
        self.assertEqual(plan.metadata["route"], "main_agent")
        self.assertNotIn("generic_data_lookup", str(plan.nodes))

    def test_plain_chat_falls_back_to_main_agent_only(self) -> None:
        provider = AutoWorkflowProvider(main_agent_provider=MainAgentWorkflowProvider())

        plan = provider.build_plan(
            OrchestrationRequest(
                task_id="task-2",
                conversation_id="conv-1",
                root_message_id="msg-1",
                user_message="你好，介绍一下你能做什么",
            )
        )

        self.assertEqual([node.capability_id for node in plan.nodes], ["main_agent.respond"])
        self.assertEqual(plan.metadata["route"], "main_agent")

    def test_resolved_question_is_used_for_main_agent_fallback_payload(self) -> None:
        provider = AutoWorkflowProvider(main_agent_provider=MainAgentWorkflowProvider())

        plan = provider.build_plan(
            OrchestrationRequest(
                task_id="task-3",
                conversation_id="conv-1",
                root_message_id="msg-1",
                user_message="那它的基因型呢？",
                resolved_user_message="查询龙粳33的基因型信息",
            )
        )

        self.assertEqual([node.capability_id for node in plan.nodes], ["main_agent.respond"])
        self.assertEqual(plan.nodes[0].input_payload["user_message"], "查询龙粳33的基因型信息")


if __name__ == "__main__":
    unittest.main()
