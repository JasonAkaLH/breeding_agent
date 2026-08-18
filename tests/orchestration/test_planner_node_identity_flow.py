from __future__ import annotations

import asyncio
import json
import unittest
from dataclasses import replace

from src.capabilities.main_agent.runtime_replanner import MainAgentRuntimeReplanner
from src.core.enums import NodeStatus, TaskStatus
from src.orchestration.backpressure import BackpressureGuard
from src.orchestration.completion_policy import CompletionPolicy
from src.orchestration.llm_workflow_provider import LLMWorkflowProvider
from src.orchestration.models import (
    CapabilityDescriptor,
    ExecutionInstance,
    InstanceState,
    OrchestrationRequest,
    WorkflowNodePlan,
    WorkflowPlan,
)
from src.orchestration.registry import CapabilityRegistry, InstanceRegistry
from src.orchestration.scheduler import Scheduler
from src.orchestration.service import OrchestrationService
from tests.orchestration.support import FakeExecutor, OrchestrationSQLiteTestCase, success_result


class _FallbackProvider:
    def build_plan(self, request: OrchestrationRequest) -> WorkflowPlan:
        return WorkflowPlan(
            task_id=request.task_id,
            nodes=(
                WorkflowNodePlan(
                    node_id=f"{request.task_id}:fallback",
                    capability_id="main_agent.respond",
                ),
            ),
        )


class PlannerNodeIdentityFlowTest(OrchestrationSQLiteTestCase):
    def _runtime(self) -> tuple[LLMWorkflowProvider, OrchestrationService]:
        registry = CapabilityRegistry()
        for descriptor in (
            CapabilityDescriptor(
                capability_id="field.inspect",
                name="Field Inspect",
                description="Inspect field evidence.",
                public=True,
            ),
            CapabilityDescriptor(
                capability_id="main_agent.respond",
                name="Main Agent",
                description="Answer the user.",
                public=True,
            ),
        ):
            registry.register(descriptor)

        async def planner(_prompt: str) -> str:
            return json.dumps(
                {
                    "nodes": [
                        {"node_id": "n1", "capability_id": "field.inspect"},
                    ]
                }
            )

        provider = LLMWorkflowProvider(
            capability_registry=registry,
            fallback_provider=_FallbackProvider(),
            macro_providers={},
            text_generator=planner,
            max_repair_attempts=0,
        )
        instances = InstanceRegistry()
        instances.register(
            ExecutionInstance(
                instance_id="inst-1",
                supported_capabilities=("field.inspect", "main_agent.respond"),
                state=InstanceState.ONLINE,
            )
        )
        service = OrchestrationService(
            storage=self.storage,
            capability_registry=registry,
            instance_registry=instances,
            scheduler=Scheduler(instances),
            executor=FakeExecutor(
                {
                    "field.inspect": success_result(output_payload={"inspected": True}),
                    "main_agent.respond": success_result(output_payload={"response_text": "done"}),
                }
            ),
            completion_policy=CompletionPolicy(),
            backpressure=BackpressureGuard(max_active_tasks=4),
        )
        return provider, service

    def test_same_model_local_ids_execute_sequentially_without_global_collision(self) -> None:
        provider, service = self._runtime()

        async def run_all():
            results = []
            for index in (1, 2):
                request = OrchestrationRequest(
                    task_id=f"task-sequential-{index}",
                    conversation_id=f"conv-{index}",
                    root_message_id=f"msg-{index}",
                    user_message="inspect",
                )
                plan = await provider.build_plan(request)
                results.append(await service.execute_request(request, plan, active_task_count=0))
            return results

        results = asyncio.run(run_all())
        first_nodes = asyncio.run(self.storage.list_task_nodes_for_task("task-sequential-1"))
        second_nodes = asyncio.run(self.storage.list_task_nodes_for_task("task-sequential-2"))

        self.assertTrue(all(result.task.status == TaskStatus.COMPLETED for result in results))
        self.assertEqual(len(first_nodes), 2)
        self.assertEqual(len(second_nodes), 2)
        self.assertTrue({node.node_id for node in first_nodes}.isdisjoint(node.node_id for node in second_nodes))
        self.assertTrue(all(":plan:v1:p0:" in node.node_id for node in (*first_nodes, *second_nodes)))

    def test_same_model_local_ids_execute_concurrently_without_global_collision(self) -> None:
        provider, service = self._runtime()
        requests = tuple(
            OrchestrationRequest(
                task_id=f"task-concurrent-{index}",
                conversation_id=f"conv-concurrent-{index}",
                root_message_id=f"msg-concurrent-{index}",
                user_message="inspect",
            )
            for index in (1, 2)
        )

        async def run_both():
            plans = await asyncio.gather(*(provider.build_plan(request) for request in requests))
            return await asyncio.gather(
                *(
                    service.execute_request(request, plan, active_task_count=0)
                    for request, plan in zip(requests, plans, strict=True)
                )
            )

        results = asyncio.run(run_both())
        node_sets = [
            {
                node.node_id
                for node in asyncio.run(self.storage.list_task_nodes_for_task(request.task_id))
            }
            for request in requests
        ]

        self.assertTrue(all(result.task.status == TaskStatus.COMPLETED for result in results))
        self.assertEqual([len(nodes) for nodes in node_sets], [2, 2])
        self.assertTrue(node_sets[0].isdisjoint(node_sets[1]))

    def test_initial_and_runtime_replan_can_reuse_n1_and_answer_user_keys(self) -> None:
        registry = CapabilityRegistry()
        for descriptor in (
            CapabilityDescriptor("field.inspect", "Field Inspect", "Inspect field evidence.", public=True),
            CapabilityDescriptor("main_agent.respond", "Main Agent", "Answer the user.", public=True),
        ):
            registry.register(descriptor)

        async def initial_planner(_prompt: str) -> str:
            return json.dumps(
                {"nodes": [{"node_id": "n1", "capability_id": "field.inspect"}]}
            )

        provider = LLMWorkflowProvider(
            capability_registry=registry,
            fallback_provider=_FallbackProvider(),
            macro_providers={},
            text_generator=initial_planner,
            max_repair_attempts=0,
        )
        request = OrchestrationRequest(
            task_id="task-replan-identity",
            conversation_id="conv-replan-identity",
            root_message_id="msg-replan-identity",
            user_message="inspect twice",
        )
        initial_plan = asyncio.run(provider.build_plan(request))
        initial_plan = replace(initial_plan, max_replans=1, max_dynamic_nodes=4)
        initial_inspect = next(node for node in initial_plan.nodes if node.capability_id == "field.inspect")

        async def replan_generator(_prompt: str, **_: object) -> str:
            return json.dumps(
                {
                    "action": "replan",
                    "reason": "inspect again",
                    "nodes": [
                        {"existing_node_id": initial_inspect.node_id},
                        {
                            "node_key": "n1",
                            "capability_id": "field.inspect",
                            "depends_on": [{"existing_node_id": initial_inspect.node_id}],
                        },
                        {
                            "node_key": "answer_user",
                            "capability_id": "main_agent.respond",
                            "depends_on": [{"node_key": "n1"}],
                        },
                    ],
                }
            )

        replanner = MainAgentRuntimeReplanner(
            capability_registry=registry,
            macro_providers={},
            text_generator=replan_generator,
            replan_claim_store=self.storage,
        )
        instances = InstanceRegistry()
        instances.register(
            ExecutionInstance(
                instance_id="inst-1",
                supported_capabilities=("field.inspect", "main_agent.respond"),
                state=InstanceState.ONLINE,
            )
        )

        def inspect_result(execution_request):
            is_initial = ":plan:v1:p0:" in execution_request.node_id
            return replace(
                success_result(
                    output_payload={
                        "satisfaction": {
                            "satisfied": not is_initial,
                            "replan_recommended": is_initial,
                            "reason_code": "inspect_again" if is_initial else "satisfied",
                        }
                    }
                ),
                capability_id=execution_request.capability_id,
                task_id=execution_request.task_id,
                node_id=execution_request.node_id,
            )

        service = OrchestrationService(
            storage=self.storage,
            capability_registry=registry,
            instance_registry=instances,
            scheduler=Scheduler(instances),
            executor=FakeExecutor(
                {
                    "field.inspect": inspect_result,
                    "main_agent.respond": success_result(output_payload={"response_text": "done"}),
                }
            ),
            completion_policy=CompletionPolicy(),
            backpressure=BackpressureGuard(max_active_tasks=2),
            runtime_replanner=replanner,
        )

        result = asyncio.run(service.execute_request(request, initial_plan, active_task_count=0))
        nodes = asyncio.run(self.storage.list_task_nodes_for_task(request.task_id))
        claim = asyncio.run(
            self.storage.get_planner_replan_claim(
                request.task_id,
                next(
                    event.payload["metadata"]["decision_digest"]
                    for event in asyncio.run(self.storage.list_events_for_task(request.task_id))
                    if event.event_type == "task.replanned"
                ),
            )
        )

        self.assertEqual(result.task.status, TaskStatus.COMPLETED)
        self.assertEqual(sum(":plan:v1:p0:n1:" in node.node_id for node in nodes), 1)
        self.assertEqual(sum(":plan:v1:r1:n1:" in node.node_id for node in nodes), 1)
        self.assertEqual(sum(":plan:v1:r1:answer_user:" in node.node_id for node in nodes), 1)
        self.assertTrue(
            any(
                ":plan:v1:p0:answer_user:" in node.node_id
                and node.status == NodeStatus.ORPHANED
                for node in nodes
            )
        )
        self.assertIsNotNone(claim)
        assert claim is not None
        self.assertEqual(claim.status, "applied")


if __name__ == "__main__":
    unittest.main()
