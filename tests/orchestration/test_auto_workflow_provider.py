from __future__ import annotations

import unittest

from src.capabilities.main_agent import MainAgentWorkflowProvider
from src.capabilities.sql_query import SQLQueryWorkflowProvider
from src.orchestration.auto_workflow_provider import AutoWorkflowProvider
from src.orchestration.models import OrchestrationRequest


class AutoWorkflowProviderTest(unittest.TestCase):
    def test_database_question_builds_sqlquery_then_main_agent_dag(self) -> None:
        provider = AutoWorkflowProvider(
            main_agent_provider=MainAgentWorkflowProvider(),
            macro_providers={"sql_query.query": SQLQueryWorkflowProvider()},
        )

        plan = provider.build_plan(
            OrchestrationRequest(
                task_id="task-1",
                conversation_id="conv-1",
                root_message_id="msg-1",
                user_message="查询龙粳18的详细审定信息",
            )
        )

        capability_ids = [node.capability_id for node in plan.nodes]
        self.assertEqual(capability_ids[:6], [
            "sql_query.intent_route",
            "sql_query.schema_context_prepare",
            "sql_query.sql_generate",
            "sql_query.sql_guard",
            "sql_query.sql_execute_readonly",
            "sql_query.result_filtering",
        ])
        self.assertEqual(capability_ids[-1], "main_agent.respond")
        self.assertEqual(plan.metadata["route"], "auto")
        self.assertEqual(plan.metadata["auto_strategy"], "deterministic_sql_query_then_main_agent")
        self.assertIn("task-1:query_data:result_filtering", plan.nodes[-1].depends_on)

    def test_plain_chat_falls_back_to_main_agent_only(self) -> None:
        provider = AutoWorkflowProvider(
            main_agent_provider=MainAgentWorkflowProvider(),
            macro_providers={"sql_query.query": SQLQueryWorkflowProvider()},
        )

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

    def test_generic_database_concept_question_stays_with_main_agent(self) -> None:
        provider = AutoWorkflowProvider(
            main_agent_provider=MainAgentWorkflowProvider(),
            macro_providers={"sql_query.query": SQLQueryWorkflowProvider()},
        )

        plan = provider.build_plan(
            OrchestrationRequest(
                task_id="task-3",
                conversation_id="conv-1",
                root_message_id="msg-1",
                user_message="数据库是什么？",
            )
        )

        self.assertEqual([node.capability_id for node in plan.nodes], ["main_agent.respond"])


if __name__ == "__main__":
    unittest.main()
