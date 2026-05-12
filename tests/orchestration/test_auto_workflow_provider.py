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

    def test_composite_database_question_builds_parallel_sqlquery_branches_then_main_agent(self) -> None:
        provider = AutoWorkflowProvider(
            main_agent_provider=MainAgentWorkflowProvider(),
            macro_providers={"sql_query.query": SQLQueryWorkflowProvider()},
        )

        plan = provider.build_plan(
            OrchestrationRequest(
                task_id="task-composite",
                conversation_id="conv-1",
                root_message_id="msg-1",
                user_message="龙粳33的审定信息和基因型信息都查一下",
            )
        )

        intent_nodes = [node for node in plan.nodes if node.capability_id == "sql_query.intent_route"]
        self.assertEqual(len(intent_nodes), 2)
        self.assertEqual(
            [node.input_payload["user_question"] for node in intent_nodes],
            ["查询龙粳33的审定信息", "查询龙粳33的基因型信息"],
        )
        self.assertTrue(all("route_hint" not in node.input_payload for node in intent_nodes))
        self.assertEqual(
            [node.input_payload["subtask_label"] for node in intent_nodes],
            ["审定信息", "基因型信息"],
        )
        self.assertEqual(plan.nodes[-1].capability_id, "main_agent.respond")
        self.assertIn("task-composite:query_approval_info:result_filtering", plan.nodes[-1].depends_on)
        self.assertIn("task-composite:query_genotype_info:result_filtering", plan.nodes[-1].depends_on)
        self.assertEqual(plan.metadata["auto_strategy"], "deterministic_sql_query_decomposed_then_main_agent")
        self.assertEqual(plan.metadata["decomposition_count"], 2)

    def test_variety_info_and_gene_info_question_builds_parallel_sqlquery_branches(self) -> None:
        provider = AutoWorkflowProvider(
            main_agent_provider=MainAgentWorkflowProvider(),
            macro_providers={"sql_query.query": SQLQueryWorkflowProvider()},
        )

        plan = provider.build_plan(
            OrchestrationRequest(
                task_id="task-variety-gene",
                conversation_id="conv-1",
                root_message_id="msg-1",
                user_message="你给我查一下龙粳33的品种信息和基因信息",
            )
        )

        intent_nodes = [node for node in plan.nodes if node.capability_id == "sql_query.intent_route"]
        self.assertEqual(len(intent_nodes), 2)
        self.assertEqual(
            [node.input_payload["user_question"] for node in intent_nodes],
            ["查询龙粳33的审定信息", "查询龙粳33的基因型信息"],
        )
        self.assertTrue(all("route_hint" not in node.input_payload for node in intent_nodes))
        self.assertEqual(plan.metadata["auto_strategy"], "deterministic_sql_query_decomposed_then_main_agent")

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
