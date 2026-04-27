from __future__ import annotations

import unittest

from src.capabilities.sql_query.workflow import (
    SQL_QUERY_INTERNAL_CAPABILITY_DESCRIPTORS,
    SQL_QUERY_PUBLIC_CAPABILITY_DESCRIPTORS,
    SQLQueryWorkflowProvider,
)
from src.orchestration.models import OrchestrationRequest


class SQLQueryPublicContractTest(unittest.TestCase):
    def test_public_descriptor_exposes_sql_query_macro_only(self) -> None:
        self.assertEqual(len(SQL_QUERY_PUBLIC_CAPABILITY_DESCRIPTORS), 1)
        descriptor = SQL_QUERY_PUBLIC_CAPABILITY_DESCRIPTORS[0]
        self.assertEqual(descriptor.capability_id, "sql_query.query")
        self.assertEqual(descriptor.name, "SQLQuery")
        self.assertTrue(descriptor.public)

    def test_internal_nodes_are_not_public(self) -> None:
        self.assertTrue(SQL_QUERY_INTERNAL_CAPABILITY_DESCRIPTORS)
        self.assertTrue(all(not descriptor.public for descriptor in SQL_QUERY_INTERNAL_CAPABILITY_DESCRIPTORS))
        self.assertTrue(all(descriptor.capability_id.startswith("sql_query.") for descriptor in SQL_QUERY_INTERNAL_CAPABILITY_DESCRIPTORS))

    def test_sql_query_provider_expands_to_internal_fixed_workflow(self) -> None:
        provider = SQLQueryWorkflowProvider()
        plan = provider.build_plan(
            OrchestrationRequest(
                task_id="task-sql-query",
                conversation_id="conv-1",
                root_message_id="msg-1",
                user_message="查询龙粳33的基因型信息",
                requested_capability_id="sql_query.query",
            )
        )

        self.assertEqual(plan.metadata["route"], "sql_query")
        self.assertEqual(plan.metadata["public_capability_id"], "sql_query.query")
        self.assertEqual(len(plan.nodes), 6)
        self.assertEqual(plan.nodes[0].capability_id, "sql_query.intent_route")
        self.assertEqual(plan.nodes[-1].capability_id, "sql_query.result_filtering")


if __name__ == "__main__":
    unittest.main()
