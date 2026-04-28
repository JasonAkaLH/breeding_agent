from __future__ import annotations

import asyncio
from dataclasses import replace

from src.capabilities.main_agent import MAIN_AGENT_CAPABILITY_DESCRIPTORS, MainAgentWorkflowProvider
from src.capabilities.sql_query import SQL_QUERY_INTERNAL_CAPABILITY_DESCRIPTORS, SQL_QUERY_PUBLIC_CAPABILITY_DESCRIPTORS, SQLQueryWorkflowProvider
from src.capabilities.sql_query.runtime_replanner import SQLQueryRuntimeReplanner
from src.core.contracts import CapabilityExecutionRequest, CapabilityExecutionResult
from src.core.enums import TaskStatus
from src.orchestration.auto_workflow_provider import AutoWorkflowProvider
from src.orchestration.backpressure import BackpressureGuard
from src.orchestration.completion_policy import CompletionPolicy
from src.orchestration.models import ExecutionInstance, InstanceState, OrchestrationRequest
from src.orchestration.registry import CapabilityRegistry, InstanceRegistry
from src.orchestration.scheduler import Scheduler
from src.orchestration.service import OrchestrationService
from tests.orchestration.support import FakeExecutor, OrchestrationSQLiteTestCase, success_result


class RuntimeSQLQueryReplanFlowTest(OrchestrationSQLiteTestCase):
    def test_auto_sql_query_plan_splits_multi_crop_request_after_single_branch_result_is_incomplete(self) -> None:
        capability_registry = CapabilityRegistry()
        for descriptor in (*MAIN_AGENT_CAPABILITY_DESCRIPTORS, *SQL_QUERY_PUBLIC_CAPABILITY_DESCRIPTORS, *SQL_QUERY_INTERNAL_CAPABILITY_DESCRIPTORS):
            capability_registry.register(descriptor)
        instance_registry = InstanceRegistry()
        instance_registry.register(
            ExecutionInstance(
                instance_id="inst-1",
                supported_capabilities=tuple(descriptor.capability_id for descriptor in (*MAIN_AGENT_CAPABILITY_DESCRIPTORS, *SQL_QUERY_INTERNAL_CAPABILITY_DESCRIPTORS)),
                state=InstanceState.ONLINE,
            )
        )
        calls: list[CapabilityExecutionRequest] = []

        def handler(request: CapabilityExecutionRequest) -> CapabilityExecutionResult:
            calls.append(request)
            if request.capability_id == "sql_query.result_filtering":
                row_count = 0 if ":query_data:" in request.node_id else 1
                satisfaction = (
                    {"satisfied": False, "reason_code": "no_relevant_rows_after_filtering", "replan_recommended": True}
                    if row_count == 0
                    else {"satisfied": True, "reason_code": "matched_rows_found", "replan_recommended": False}
                )
                return replace(
                    success_result(output_payload={"row_count": row_count, "filter_reason": "initial branch incomplete", "satisfaction": satisfaction}),
                    capability_id=request.capability_id,
                    task_id=request.task_id,
                    node_id=request.node_id,
                )
            if request.capability_id == "main_agent.respond":
                return replace(success_result(output_payload={"answer": "ok"}), capability_id=request.capability_id, task_id=request.task_id, node_id=request.node_id)
            return replace(success_result(output_payload={"ok": True}), capability_id=request.capability_id, task_id=request.task_id, node_id=request.node_id)

        executor = FakeExecutor({capability_id: handler for capability_id in tuple(descriptor.capability_id for descriptor in (*MAIN_AGENT_CAPABILITY_DESCRIPTORS, *SQL_QUERY_INTERNAL_CAPABILITY_DESCRIPTORS))})
        sql_query_provider = SQLQueryWorkflowProvider()
        macro_providers = {"sql_query.query": sql_query_provider}
        service = OrchestrationService(
            storage=self.storage,
            capability_registry=capability_registry,
            instance_registry=instance_registry,
            scheduler=Scheduler(instance_registry),
            executor=executor,
            completion_policy=CompletionPolicy(),
            backpressure=BackpressureGuard(max_active_tasks=2),
            runtime_replanner=SQLQueryRuntimeReplanner(macro_providers=macro_providers),
        )
        workflow_provider = AutoWorkflowProvider(
            main_agent_provider=MainAgentWorkflowProvider(),
            macro_providers=macro_providers,
        )
        request = OrchestrationRequest(
            task_id="task-multi-crop",
            conversation_id="conv-1",
            root_message_id="msg-1",
            user_message="你帮我查一下适合河南种的水稻和适合浙江中的玉米\n补充信息：route_id=审定品种库",
        )

        result = asyncio.run(service.execute_request(request, workflow_provider.build_plan(request), active_task_count=0))
        nodes = asyncio.run(self.storage.list_task_nodes_for_task("task-multi-crop"))
        events = asyncio.run(self.storage.list_events_for_task("task-multi-crop"))

        self.assertEqual(result.task.status, TaskStatus.COMPLETED)
        dynamic_filter_nodes = [node for node in nodes if "runtime_query_1_" in node.node_id and node.capability_id == "sql_query.result_filtering"]
        self.assertEqual(len(dynamic_filter_nodes), 2)
        self.assertTrue(all(str(node.status) == "completed" for node in dynamic_filter_nodes))
        self.assertTrue(any(event.event_type == "task.replanned" for event in events))
        self.assertTrue(any(event.event_type == "task.graph_updated" for event in events))
        self.assertFalse(any(call.capability_id == "main_agent.respond" and call.node_id.endswith(":main_agent.respond") for call in calls))
        dynamic_intent_calls = [call for call in calls if call.capability_id == "sql_query.intent_route" and "runtime_query_1_" in call.node_id]
        self.assertEqual(
            [call.input_payload.get("user_question") for call in dynamic_intent_calls],
            [
                "查询适合河南种植的水稻\n补充信息：route_id=审定品种库",
                "查询适合浙江种植的玉米\n补充信息：route_id=审定品种库",
            ],
        )


if __name__ == "__main__":
    import unittest

    unittest.main()
