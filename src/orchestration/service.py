from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from src.core.contracts import CapabilityExecutionRequest, EventSink, ExecutorPort, StoragePort
from src.core.enums import EventVisibility, NodeStatus, TaskStatus
from src.core.models import EventRecord, Task, TaskEdge, TaskNode

from .backpressure import BackpressureGuard
from .completion_policy import CompletionPolicy, CompletionStatus
from .models import OrchestrationRequest, OrchestrationRunResult, WorkflowNodePlan, WorkflowPlan
from .registry import CapabilityRegistry, InstanceRegistry
from .scheduler import Scheduler


class OrchestrationService:
    def __init__(
        self,
        *,
        storage: StoragePort,
        capability_registry: CapabilityRegistry,
        instance_registry: InstanceRegistry,
        scheduler: Scheduler,
        executor: ExecutorPort,
        completion_policy: CompletionPolicy,
        backpressure: BackpressureGuard,
        event_sink: EventSink | None = None,
    ) -> None:
        self._storage = storage
        self._capability_registry = capability_registry
        self._instance_registry = instance_registry
        self._scheduler = scheduler
        self._executor = executor
        self._completion_policy = completion_policy
        self._backpressure = backpressure
        self._event_sink = event_sink
        self._event_counter = 0

    @staticmethod
    def _utcnow_naive() -> datetime:
        return datetime.now(timezone.utc).replace(tzinfo=None)

    def _make_event(
        self,
        *,
        task_id: str,
        conversation_id: str,
        event_type: str,
        node_id: str | None = None,
        payload: dict[str, Any] | None = None,
        visibility: EventVisibility = EventVisibility.FRONTEND,
    ) -> EventRecord:
        self._event_counter += 1
        return EventRecord(
            event_id=f"evt-{task_id}-{self._event_counter}",
            conversation_id=conversation_id,
            task_id=task_id,
            node_id=node_id,
            event_type=event_type,
            payload=payload or {},
            visibility=visibility,
            created_at=self._utcnow_naive(),
        )

    async def _record_event(self, event: EventRecord) -> None:
        await self._storage.append_event(event)
        if self._event_sink is not None:
            await self._event_sink.publish(event)

    async def _initialize_task(self, request: OrchestrationRequest, plan: WorkflowPlan) -> Task:
        now = self._utcnow_naive()
        root_node_id = next((node.node_id for node in plan.nodes if not node.depends_on), None)
        existing_task = await self._storage.get_task(request.task_id)
        existing_nodes = {
            node.node_id: node
            for node in await self._storage.list_task_nodes_for_task(plan.task_id)
        }
        planning_task = (
            replace(
                existing_task,
                status=TaskStatus.PLANNING,
                requested_capability_id=request.requested_capability_id,
                root_node_id=root_node_id,
                summary=request.user_message,
                updated_at=now,
            )
            if existing_task is not None
            else Task(
                task_id=request.task_id,
                conversation_id=request.conversation_id,
                root_message_id=request.root_message_id,
                status=TaskStatus.PLANNING,
                requested_capability_id=request.requested_capability_id,
                root_node_id=root_node_id,
                summary=request.user_message,
                created_at=now,
                updated_at=now,
            )
        )
        saved = await self._storage.save_task(planning_task)
        for node in plan.nodes:
            if node.node_id not in existing_nodes:
                await self._storage.save_task_node(
                    TaskNode(
                        node_id=node.node_id,
                        task_id=plan.task_id,
                        capability_id=node.capability_id,
                        status=NodeStatus.PENDING,
                        criticality=node.criticality,
                        retry_policy=dict(node.retry_policy),
                        timeout_policy=dict(node.timeout_policy),
                        resource_class=node.resource_class,
                    )
                )
            for dependency in node.depends_on:
                await self._storage.save_task_edge(
                    plan.task_id,
                    TaskEdge(from_node_id=dependency, to_node_id=node.node_id),
                )
        running = replace(saved, status=TaskStatus.RUNNING, updated_at=self._utcnow_naive())
        running = await self._storage.save_task(running)
        await self._record_event(
            self._make_event(
                task_id=request.task_id,
                conversation_id=request.conversation_id,
                event_type="task.graph_created",
                payload={
                    "node_count": len(plan.nodes),
                    "edge_count": sum(len(node.depends_on) for node in plan.nodes),
                    "root_node_id": root_node_id,
                },
            )
        )
        return running

    async def _execute_node(
        self,
        request: OrchestrationRequest,
        node_plan: WorkflowNodePlan,
        task_node: TaskNode,
        *,
        dependency_outputs: dict[str, dict[str, Any]],
    ) -> tuple[TaskNode, dict[str, Any]]:
        instance = self._scheduler.select_instance(node_plan.capability_id)
        running = replace(
            task_node,
            status=NodeStatus.RUNNING,
            assigned_instance_id=instance.instance_id,
            started_at=task_node.started_at or self._utcnow_naive(),
        )
        running = await self._storage.save_task_node(running)
        await self._record_event(
            self._make_event(
                task_id=request.task_id,
                conversation_id=request.conversation_id,
                node_id=task_node.node_id,
                event_type="node.started",
                payload={"capability_id": node_plan.capability_id, "instance_id": instance.instance_id},
            )
        )

        result = await self._executor.execute(
            CapabilityExecutionRequest(
                capability_id=node_plan.capability_id,
                conversation_id=request.conversation_id,
                task_id=request.task_id,
                node_id=task_node.node_id,
                input_payload=dict(node_plan.input_payload),
                dependency_outputs={dependency: dict(dependency_outputs.get(dependency, {})) for dependency in node_plan.depends_on},
                metadata=dict(request.metadata),
            )
        )

        latest_task = await self._storage.get_task(request.task_id)
        latest_node = await self._storage.get_task_node(task_node.node_id) or running
        if latest_task is not None and latest_task.status in {TaskStatus.CANCELLING, TaskStatus.CANCELLED}:
            await self._record_event(
                self._make_event(
                    task_id=request.task_id,
                    conversation_id=request.conversation_id,
                    node_id=task_node.node_id,
                    event_type="task.late_result_discarded",
                    payload={"capability_id": node_plan.capability_id},
                    visibility=EventVisibility.AUDIT_ONLY,
                )
            )
            return latest_node, {}

        for artifact in result.artifacts:
            await self._storage.save_artifact(artifact)
        for event in result.events:
            await self._record_event(event)

        now = self._utcnow_naive()
        if result.interrupt is not None:
            await self._storage.save_interrupt(result.interrupt)
            updated = replace(latest_node, status=NodeStatus.WAITING_FOR_INPUT)
            return await self._storage.save_task_node(updated), dict(result.output_payload)

        if result.error is not None:
            failed = replace(latest_node, status=NodeStatus.FAILED, finished_at=now)
            failed = await self._storage.save_task_node(failed)
            await self._record_event(
                self._make_event(
                    task_id=request.task_id,
                    conversation_id=request.conversation_id,
                    node_id=task_node.node_id,
                    event_type="node.failed",
                    payload={"code": result.error.code},
                )
            )
            return failed, dict(result.output_payload)

        completed = replace(
            latest_node,
            status=NodeStatus.COMPLETED,
            finished_at=now,
            output_refs=tuple(artifact.artifact_id for artifact in result.artifacts),
        )
        completed = await self._storage.save_task_node(completed)
        await self._record_event(
            self._make_event(
                task_id=request.task_id,
                conversation_id=request.conversation_id,
                node_id=task_node.node_id,
                event_type="node.completed",
                payload={"capability_id": node_plan.capability_id},
            )
        )
        return completed, dict(result.output_payload)

    async def execute_request(self, request: OrchestrationRequest, plan: WorkflowPlan, *, active_task_count: int) -> OrchestrationRunResult:
        self._backpressure.ensure_can_accept(active_task_count=active_task_count)
        for node in plan.nodes:
            self._capability_registry.require(node.capability_id)

        existing_task = await self._storage.get_task(request.task_id)
        if existing_task is not None and existing_task.status in {TaskStatus.CANCELLING, TaskStatus.CANCELLED}:
            return OrchestrationRunResult(task=existing_task, nodes=(), completion_status=existing_task.status.value)

        task = await self._initialize_task(request, plan)
        replan_count = 0
        node_outputs: dict[str, dict[str, Any]] = {}

        while True:
            current_task = await self._storage.get_task(task.task_id)
            if current_task is not None and current_task.status in {TaskStatus.CANCELLING, TaskStatus.CANCELLED}:
                return OrchestrationRunResult(
                    task=current_task,
                    nodes=tuple(await self._storage.list_task_nodes_for_task(plan.task_id)),
                    completion_status=current_task.status.value,
                )
            nodes = {node.node_id: node for node in await self._storage.list_task_nodes_for_task(plan.task_id)}
            progress_made = False
            completed_ids = {node_id for node_id, node in nodes.items() if node.status == NodeStatus.COMPLETED}

            for node_plan in plan.nodes:
                refreshed_task = await self._storage.get_task(task.task_id)
                if refreshed_task is not None and refreshed_task.status in {TaskStatus.CANCELLING, TaskStatus.CANCELLED}:
                    return OrchestrationRunResult(
                        task=refreshed_task,
                        nodes=tuple(await self._storage.list_task_nodes_for_task(plan.task_id)),
                        completion_status=refreshed_task.status.value,
                    )

                node = await self._storage.get_task_node(node_plan.node_id) or nodes[node_plan.node_id]
                nodes[node.node_id] = node
                if node.status in {
                    NodeStatus.COMPLETED,
                    NodeStatus.FAILED,
                    NodeStatus.CANCELLED,
                    NodeStatus.BLOCKED_BY_CANCELLATION,
                    NodeStatus.ORPHANED,
                    NodeStatus.WAITING_FOR_INPUT,
                }:
                    continue

                if any(dep not in completed_ids for dep in node_plan.depends_on):
                    if node.status == NodeStatus.PENDING:
                        updated = await self._storage.save_task_node(replace(node, status=NodeStatus.WAITING_FOR_DEPENDENCY))
                        nodes[node.node_id] = updated
                    continue

                if node.status in {
                    NodeStatus.PENDING,
                    NodeStatus.WAITING_FOR_DEPENDENCY,
                    NodeStatus.READY_TO_RESUME,
                    NodeStatus.RESUMING,
                }:
                    node = await self._storage.save_task_node(replace(node, status=NodeStatus.READY))
                    nodes[node.node_id] = node

                if node.status == NodeStatus.READY:
                    updated, output_payload = await self._execute_node(
                        request,
                        node_plan,
                        node,
                        dependency_outputs=node_outputs,
                    )
                    nodes[node.node_id] = updated
                    node_outputs[node.node_id] = output_payload
                    progress_made = True
                    if updated.status == NodeStatus.COMPLETED:
                        completed_ids.add(updated.node_id)

            unresolved_interrupt = any(interrupt.status == "open" for interrupt in await self._storage.list_interrupts_for_task(plan.task_id))
            completion = self._completion_policy.evaluate(
                plan,
                {node_id: node.status for node_id, node in nodes.items()},
                replan_count=replan_count,
                unresolved_interrupt=unresolved_interrupt,
            )

            if completion == CompletionStatus.COMPLETED:
                task = await self._storage.save_task(replace(task, status=TaskStatus.COMPLETED, updated_at=self._utcnow_naive()))
                await self._record_event(
                    self._make_event(task_id=task.task_id, conversation_id=task.conversation_id, event_type="task.completed")
                )
                return OrchestrationRunResult(task=task, nodes=tuple(nodes.values()), completion_status=completion.value)

            if completion == CompletionStatus.FAILED:
                task = await self._storage.save_task(replace(task, status=TaskStatus.FAILED, updated_at=self._utcnow_naive()))
                await self._record_event(
                    self._make_event(task_id=task.task_id, conversation_id=task.conversation_id, event_type="task.failed")
                )
                return OrchestrationRunResult(task=task, nodes=tuple(nodes.values()), completion_status=completion.value)

            if completion == CompletionStatus.REPLAN_AVAILABLE:
                task = await self._storage.save_task(replace(task, status=TaskStatus.FAILED, updated_at=self._utcnow_naive()))
                await self._record_event(
                    self._make_event(
                        task_id=task.task_id,
                        conversation_id=task.conversation_id,
                        event_type="task.replan_available",
                    )
                )
                return OrchestrationRunResult(task=task, nodes=tuple(nodes.values()), completion_status=completion.value)

            if completion == CompletionStatus.WAITING_FOR_INPUT or not progress_made:
                task = await self._storage.save_task(replace(task, status=TaskStatus.RUNNING, updated_at=self._utcnow_naive()))
                return OrchestrationRunResult(task=task, nodes=tuple(nodes.values()), completion_status=completion.value)
