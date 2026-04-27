from __future__ import annotations

import asyncio
import json

from src.capabilities.sql_query import SQL_QUERY_INTERNAL_CAPABILITY_DESCRIPTORS, SQLQueryExecutor, SQLQueryWorkflowProvider, build_local_sql_query_instance
from src.integrations.mysql_readonly import MySQLReadonlyAdapter
from src.orchestration.backpressure import BackpressureGuard
from src.orchestration.completion_policy import CompletionPolicy
from src.orchestration.models import OrchestrationRequest
from src.orchestration.registry import CapabilityRegistry, InstanceRegistry
from src.orchestration.scheduler import Scheduler
from src.orchestration.service import OrchestrationService
from tests.orchestration.support import OrchestrationSQLiteTestCase


class SQLQueryOrchestrationFlowTest(OrchestrationSQLiteTestCase):
    def test_sql_query_capability_chain_closes_via_orchestration(self) -> None:
        capability_registry = CapabilityRegistry()
        for descriptor in SQL_QUERY_INTERNAL_CAPABILITY_DESCRIPTORS:
            capability_registry.register(descriptor)

        instance_registry = InstanceRegistry()
        instance_registry.register(build_local_sql_query_instance())

        adapter = MySQLReadonlyAdapter(
            runner=lambda sql: type("Result", (), {"columns": ("variety_name",), "rows": ({"variety_name": "龙粳33"},), "row_count": 1})()
        )
        executor = SQLQueryExecutor(mysql_adapter=adapter)
        service = OrchestrationService(
            storage=self.storage,
            capability_registry=capability_registry,
            instance_registry=instance_registry,
            scheduler=Scheduler(instance_registry),
            executor=executor,
            completion_policy=CompletionPolicy(),
            backpressure=BackpressureGuard(max_active_tasks=2),
        )
        provider = SQLQueryWorkflowProvider()
        request = OrchestrationRequest(
            task_id="task-sql_query-1",
            conversation_id="conv-1",
            root_message_id="msg-1",
            user_message="查询某个品种的基因型信息",
        )

        result = asyncio.run(service.execute_request(request, provider.build_plan(request), active_task_count=0))
        nodes = asyncio.run(self.storage.list_task_nodes_for_task("task-sql_query-1"))

        self.assertEqual(result.task.status, "completed")
        self.assertEqual(len(nodes), 6)
        self.assertTrue(all(node.status == "completed" for node in nodes))

    def test_llm_generated_sql_still_flows_through_guard(self) -> None:
        capability_registry = CapabilityRegistry()
        for descriptor in SQL_QUERY_INTERNAL_CAPABILITY_DESCRIPTORS:
            capability_registry.register(descriptor)

        instance_registry = InstanceRegistry()
        instance_registry.register(build_local_sql_query_instance())

        async def unsafe_llm(_: str) -> str:
            return json.dumps(
                {
                    "mode": "answer",
                    "route_id": "genotype_db",
                    "schema_profile_id": "genotype_profile",
                    "sql": "SELECT variety.variety_id FROM variety",
                    "tables_used": ["variety"],
                    "columns_used": ["variety.variety_id"],
                    "column_types_used": {"variety.variety_id": "int(11)"},
                    "join_hints_used": [],
                }
            )

        executor = SQLQueryExecutor(llm_text_generator=unsafe_llm)
        service = OrchestrationService(
            storage=self.storage,
            capability_registry=capability_registry,
            instance_registry=instance_registry,
            scheduler=Scheduler(instance_registry),
            executor=executor,
            completion_policy=CompletionPolicy(),
            backpressure=BackpressureGuard(max_active_tasks=2),
        )
        provider = SQLQueryWorkflowProvider()
        request = OrchestrationRequest(
            task_id="task-sql_query-llm-guard",
            conversation_id="conv-1",
            root_message_id="msg-1",
            user_message="查询品种龙粳33的基因型信息",
        )

        result = asyncio.run(service.execute_request(request, provider.build_plan(request), active_task_count=0))
        nodes = asyncio.run(self.storage.list_task_nodes_for_task("task-sql_query-llm-guard"))
        events = asyncio.run(self.storage.list_events_for_task("task-sql_query-llm-guard"))

        self.assertEqual(result.task.status, "failed")
        guard_node = next(node for node in nodes if node.node_id.endswith(":sql_guard"))
        self.assertEqual(guard_node.status, "failed")
        self.assertTrue(any(event.event_type == "sql_query.sql_guard_blocked" for event in events))
