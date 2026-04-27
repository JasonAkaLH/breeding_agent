from __future__ import annotations

import unittest

from src.capabilities.sql_query import SQLQueryWorkflowProvider
from src.orchestration.models import OrchestrationRequest, WorkflowNodePlan, WorkflowPlan
from src.orchestration.workflow_expander import WorkflowExpander


class WorkflowExpanderTest(unittest.TestCase):
    def test_expands_sql_query_macro_and_rewires_downstream_dependencies(self) -> None:
        request = OrchestrationRequest(
            task_id="task-1",
            conversation_id="conv-1",
            root_message_id="msg-1",
            user_message="查询龙粳33的基因型信息",
        )
        high_level = WorkflowPlan(
            task_id="task-1",
            nodes=(
                WorkflowNodePlan(node_id="query_data", capability_id="sql_query.query"),
                WorkflowNodePlan(node_id="answer_user", capability_id="main_agent.respond", depends_on=("query_data",)),
            ),
        )

        expanded = WorkflowExpander({"sql_query.query": SQLQueryWorkflowProvider()}).expand(high_level, request=request)

        self.assertEqual(expanded.task_id, "task-1")
        self.assertEqual(len(expanded.nodes), 7)
        capability_ids = [node.capability_id for node in expanded.nodes]
        self.assertEqual(capability_ids[:6], [
            "sql_query.intent_route",
            "sql_query.schema_context_prepare",
            "sql_query.sql_generate",
            "sql_query.sql_guard",
            "sql_query.sql_execute_readonly",
            "sql_query.result_filtering",
        ])
        self.assertEqual(capability_ids[-1], "main_agent.respond")
        self.assertIn("task-1:query_data:result_filtering", expanded.nodes[-1].depends_on)
        self.assertNotIn("query_data", expanded.nodes[-1].depends_on)

    def test_macro_roots_depend_on_high_level_dependencies(self) -> None:
        request = OrchestrationRequest(
            task_id="task-2",
            conversation_id="conv-1",
            root_message_id="msg-1",
            user_message="查询龙粳33",
        )
        high_level = WorkflowPlan(
            task_id="task-2",
            nodes=(
                WorkflowNodePlan(node_id="prepare_context", capability_id="main_agent.respond"),
                WorkflowNodePlan(node_id="query_data", capability_id="sql_query.query", depends_on=("prepare_context",)),
            ),
        )

        expanded = WorkflowExpander({"sql_query.query": SQLQueryWorkflowProvider()}).expand(high_level, request=request)
        intent_node = next(node for node in expanded.nodes if node.capability_id == "sql_query.intent_route")

        self.assertEqual(intent_node.depends_on, ("prepare_context",))


if __name__ == "__main__":
    unittest.main()
