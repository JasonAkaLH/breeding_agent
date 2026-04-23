from __future__ import annotations

import asyncio

from src.capabilities.nl2sql import NL2SQL_CAPABILITY_DESCRIPTORS, NL2SQLExecutor, NL2SQLWorkflowProvider, build_local_nl2sql_instance
from src.integrations.mysql_readonly import MySQLReadonlyAdapter
from src.orchestration.backpressure import BackpressureGuard
from src.orchestration.completion_policy import CompletionPolicy
from src.orchestration.models import OrchestrationRequest
from src.orchestration.registry import CapabilityRegistry, InstanceRegistry
from src.orchestration.scheduler import Scheduler
from src.orchestration.service import OrchestrationService
from tests.orchestration.support import OrchestrationSQLiteTestCase


class NL2SQLOrchestrationFlowTest(OrchestrationSQLiteTestCase):
    def test_nl2sql_capability_chain_closes_via_orchestration(self) -> None:
        capability_registry = CapabilityRegistry()
        for descriptor in NL2SQL_CAPABILITY_DESCRIPTORS:
            capability_registry.register(descriptor)

        instance_registry = InstanceRegistry()
        instance_registry.register(build_local_nl2sql_instance())

        adapter = MySQLReadonlyAdapter(
            runner=lambda sql: type("Result", (), {"columns": ("variety_name",), "rows": ({"variety_name": "先玉335"},), "row_count": 1})()
        )
        executor = NL2SQLExecutor(mysql_adapter=adapter)
        service = OrchestrationService(
            storage=self.storage,
            capability_registry=capability_registry,
            instance_registry=instance_registry,
            scheduler=Scheduler(instance_registry),
            executor=executor,
            completion_policy=CompletionPolicy(),
            backpressure=BackpressureGuard(max_active_tasks=2),
        )
        provider = NL2SQLWorkflowProvider()
        request = OrchestrationRequest(
            task_id="task-nl2sql-1",
            conversation_id="conv-1",
            root_message_id="msg-1",
            user_message="查询某个品种的基因型信息",
        )

        result = asyncio.run(service.execute_request(request, provider.build_plan(request), active_task_count=0))
        nodes = asyncio.run(self.storage.list_task_nodes_for_task("task-nl2sql-1"))

        self.assertEqual(result.task.status, "completed")
        self.assertEqual(len(nodes), 6)
        self.assertTrue(all(node.status == "completed" for node in nodes))
