from __future__ import annotations

import unittest

from src.capabilities.main_agent import MainAgentWorkflowProvider
from src.capabilities.sql_query import SQLQueryWorkflowProvider
from src.orchestration.models import OrchestrationRequest
from src.orchestration.workflow_router import WorkflowRouter


class WorkflowRouterTest(unittest.TestCase):
    def test_routes_sql_query_public_capability_to_sql_query_provider(self) -> None:
        router = WorkflowRouter(
            default_provider=MainAgentWorkflowProvider(),
            main_agent_provider=MainAgentWorkflowProvider(),
            sql_query_provider=SQLQueryWorkflowProvider(),
        )
        plan = router.build_plan(
            OrchestrationRequest(
                task_id="task-1",
                conversation_id="conv-1",
                root_message_id="msg-1",
                user_message="查询数据",
                requested_capability_id="sql_query.query",
            )
        )
        self.assertEqual(plan.metadata["route"], "sql_query")

    def test_top_level_sql_query_alias_routes_to_sql_query_provider(self) -> None:
        router = WorkflowRouter(
            default_provider=MainAgentWorkflowProvider(),
            main_agent_provider=MainAgentWorkflowProvider(),
            sql_query_provider=SQLQueryWorkflowProvider(),
        )
        plan = router.build_plan(
            OrchestrationRequest(
                task_id="task-sql-query-alias",
                conversation_id="conv-1",
                root_message_id="msg-1",
                user_message="查询数据",
                requested_capability_id="sql_query",
            )
        )
        self.assertEqual(plan.metadata["route"], "sql_query")


if __name__ == "__main__":
    unittest.main()
