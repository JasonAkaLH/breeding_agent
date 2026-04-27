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
