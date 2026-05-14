from __future__ import annotations

import asyncio
from datetime import datetime

from src.core.contracts import CapabilityExecutionResult
from src.core.enums import TaskStatus
from src.core.models import EventRecord, Task
from src.orchestration.backpressure import BackpressureGuard
from src.orchestration.completion_policy import CompletionPolicy
from src.orchestration.models import CapabilityDescriptor, ExecutionInstance, InstanceState, OrchestrationRequest, WorkflowNodePlan, WorkflowPlan
from src.orchestration.registry import CapabilityRegistry, InstanceRegistry
from src.orchestration.scheduler import Scheduler
from src.orchestration.service import OrchestrationService
from tests.orchestration.support import FakeExecutor, OrchestrationSQLiteTestCase, error_result, success_result


class FakeCapabilityFlowTest(OrchestrationSQLiteTestCase):
    def test_fake_capability_flow_runs_to_completion(self) -> None:
        capability_registry = CapabilityRegistry()
        capability_registry.register(CapabilityDescriptor(capability_id="cap.route", name="route", description="route"))
        capability_registry.register(CapabilityDescriptor(capability_id="cap.respond", name="respond", description="respond"))

        instance_registry = InstanceRegistry()
        instance_registry.register(ExecutionInstance(instance_id="inst-1", supported_capabilities=("cap.route", "cap.respond"), state=InstanceState.ONLINE, load_score=0))

        executor = FakeExecutor(
            {
                "cap.route": success_result(output_payload={"route": "default"}),
                "cap.respond": success_result(output_payload={"answer": "done"}),
            }
        )

        service = OrchestrationService(
            storage=self.storage,
            capability_registry=capability_registry,
            instance_registry=instance_registry,
            scheduler=Scheduler(instance_registry),
            executor=executor,
            completion_policy=CompletionPolicy(),
            backpressure=BackpressureGuard(max_active_tasks=2),
        )

        request = OrchestrationRequest(task_id="task-1", conversation_id="conv-1", root_message_id="msg-1", user_message="hello")
        plan = WorkflowPlan(
            task_id="task-1",
            nodes=(
                WorkflowNodePlan(node_id="node-1", capability_id="cap.route"),
                WorkflowNodePlan(node_id="node-2", capability_id="cap.respond", depends_on=("node-1",)),
            ),
        )

        result = asyncio.run(service.execute_request(request, plan, active_task_count=0))
        stored_task = asyncio.run(self.storage.get_task("task-1"))
        stored_nodes = asyncio.run(self.storage.list_task_nodes_for_task("task-1"))
        stored_events = asyncio.run(self.storage.list_events_for_task("task-1"))

        self.assertEqual(result.task.status, TaskStatus.COMPLETED)
        self.assertEqual(stored_task.status, TaskStatus.COMPLETED)
        self.assertEqual([node.status for node in stored_nodes], ["completed", "completed"])
        self.assertGreaterEqual(len(stored_events), 4)

    def test_executor_events_without_timestamp_are_persisted_with_created_at(self) -> None:
        capability_registry = CapabilityRegistry()
        capability_registry.register(CapabilityDescriptor(capability_id="cap.respond", name="respond", description="respond"))

        instance_registry = InstanceRegistry()
        instance_registry.register(ExecutionInstance(instance_id="inst-1", supported_capabilities=("cap.respond",), state=InstanceState.ONLINE, load_score=0))

        def handler(request):
            return CapabilityExecutionResult(
                capability_id=request.capability_id,
                task_id=request.task_id,
                node_id=request.node_id,
                output_payload={"answer": "done"},
                events=(EventRecord(
                    event_id="custom-progress",
                    conversation_id=request.conversation_id,
                    task_id=request.task_id,
                    node_id=request.node_id,
                    event_type="custom.progress",
                    payload={"step": "working"},
                ),),
            )

        service = OrchestrationService(
            storage=self.storage,
            capability_registry=capability_registry,
            instance_registry=instance_registry,
            scheduler=Scheduler(instance_registry),
            executor=FakeExecutor({"cap.respond": handler}),
            completion_policy=CompletionPolicy(),
            backpressure=BackpressureGuard(max_active_tasks=2),
        )

        request = OrchestrationRequest(task_id="task-event-time", conversation_id="conv-1", root_message_id="msg-1", user_message="hello")
        plan = WorkflowPlan(task_id="task-event-time", nodes=(WorkflowNodePlan(node_id="node-1", capability_id="cap.respond"),))

        asyncio.run(service.execute_request(request, plan, active_task_count=0))
        stored_events = asyncio.run(self.storage.list_events_for_task("task-event-time"))

        custom = next(event for event in stored_events if event.event_id == "custom-progress")
        self.assertIsNotNone(custom.created_at)

    def test_node_started_frontend_event_exposes_skill_name_from_plan_metadata(self) -> None:
        capability_registry = CapabilityRegistry()
        capability_registry.register(CapabilityDescriptor(capability_id="skill.demo_query", name="demo", description="demo"))

        instance_registry = InstanceRegistry()
        instance_registry.register(ExecutionInstance(instance_id="inst-1", supported_capabilities=("skill.demo_query",), state=InstanceState.ONLINE, load_score=0))

        service = OrchestrationService(
            storage=self.storage,
            capability_registry=capability_registry,
            instance_registry=instance_registry,
            scheduler=Scheduler(instance_registry),
            executor=FakeExecutor({"skill.demo_query": success_result(output_payload={"answer": "done"})}),
            completion_policy=CompletionPolicy(),
            backpressure=BackpressureGuard(max_active_tasks=2),
        )

        request = OrchestrationRequest(task_id="task-skill-name", conversation_id="conv-1", root_message_id="msg-1", user_message="hello")
        plan = WorkflowPlan(
            task_id="task-skill-name",
            nodes=(WorkflowNodePlan(node_id="node-1", capability_id="skill.demo_query", metadata={"skill_name": "demo-query"}),),
        )

        asyncio.run(service.execute_request(request, plan, active_task_count=0))
        stored_events = asyncio.run(self.storage.list_events_for_task("task-skill-name"))

        started = next(event for event in stored_events if event.event_type == "node.started")
        completed = next(event for event in stored_events if event.event_type == "node.completed")
        self.assertEqual(started.payload["capability_id"], "skill.demo_query")
        self.assertEqual(started.payload["skill_name"], "demo-query")
        self.assertEqual(completed.payload["capability_id"], "skill.demo_query")
        self.assertEqual(completed.payload["skill_name"], "demo-query")

    def test_required_failure_does_not_complete_task(self) -> None:
        capability_registry = CapabilityRegistry()
        capability_registry.register(CapabilityDescriptor(capability_id="cap.route", name="route", description="route"))

        instance_registry = InstanceRegistry()
        instance_registry.register(ExecutionInstance(instance_id="inst-1", supported_capabilities=("cap.route",), state=InstanceState.ONLINE, load_score=0))

        executor = FakeExecutor({"cap.route": error_result(code="boom", message="failed")})

        service = OrchestrationService(
            storage=self.storage,
            capability_registry=capability_registry,
            instance_registry=instance_registry,
            scheduler=Scheduler(instance_registry),
            executor=executor,
            completion_policy=CompletionPolicy(),
            backpressure=BackpressureGuard(max_active_tasks=2),
        )

        request = OrchestrationRequest(task_id="task-2", conversation_id="conv-1", root_message_id="msg-2", user_message="hello")
        plan = WorkflowPlan(task_id="task-2", nodes=(WorkflowNodePlan(node_id="node-1", capability_id="cap.route"),), max_replans=0)

        result = asyncio.run(service.execute_request(request, plan, active_task_count=0))
        self.assertEqual(result.task.status, TaskStatus.FAILED)
