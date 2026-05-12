from __future__ import annotations

import unittest

from src.capabilities.sql_query.workflow import SQLQueryWorkflowProvider
from src.orchestration.models import OrchestrationRequest


class SQLQueryWorkflowProviderTest(unittest.TestCase):
    def test_builds_standard_six_node_workflow(self) -> None:
        provider = SQLQueryWorkflowProvider()
        request = OrchestrationRequest(task_id="task-1", conversation_id="conv-1", root_message_id="msg-1", user_message="查询某品种的基因型信息")

        plan = provider.build_plan(request)

        self.assertEqual(len(plan.nodes), 6)
        self.assertEqual(plan.nodes[0].capability_id, "sql_query.intent_route")
        self.assertEqual(plan.nodes[1].depends_on, (plan.nodes[0].node_id,))
        self.assertEqual(plan.nodes[2].depends_on, (plan.nodes[0].node_id, plan.nodes[1].node_id))
        self.assertEqual(plan.nodes[3].depends_on, (plan.nodes[2].node_id,))
        self.assertEqual(plan.nodes[4].depends_on, (plan.nodes[3].node_id,))
        self.assertEqual(plan.nodes[4].capability_id, "sql_query.sql_execute_readonly")
        self.assertEqual(plan.nodes[5].depends_on, (plan.nodes[4].node_id, plan.nodes[2].node_id))
        self.assertEqual(plan.nodes[5].capability_id, "sql_query.result_filtering")
        self.assertEqual(plan.max_replans, 1)
        self.assertEqual(plan.max_dynamic_nodes, 24)

    def test_macro_route_hint_is_not_forwarded_to_internal_intent_route(self) -> None:
        provider = SQLQueryWorkflowProvider()
        request = OrchestrationRequest(
            task_id="task-hint",
            conversation_id="conv-1",
            root_message_id="msg-1",
            user_message="查询龙粳33的基因型信息",
            metadata={
                "macro_input_payload": {
                    "route_hint": "genotype_db",
                    "subtask_label": "基因型信息",
                    "parent_question": "龙粳33的审定信息和基因型信息都查一下",
                }
            },
        )

        plan = provider.build_plan(request)

        intent_payload = plan.nodes[0].input_payload
        self.assertEqual(intent_payload["user_question"], "查询龙粳33的基因型信息")
        self.assertNotIn("route_hint", intent_payload)
        self.assertEqual(intent_payload["subtask_label"], "基因型信息")
        self.assertEqual(intent_payload["parent_question"], "龙粳33的审定信息和基因型信息都查一下")
