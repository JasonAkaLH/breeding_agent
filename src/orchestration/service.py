from __future__ import annotations

import inspect
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
from .runtime_replanner import NoopRuntimeReplanner, RuntimeReplanContext, RuntimeReplanDecision, RuntimeReplanner
from .scheduler import Scheduler
from .workflow_plan_validator import WorkflowPlanValidator

_SYSTEM_NODE_METADATA_KEYS = frozenset(
    {
        "forced_skill_capability_id",
        "forced_skill_name",
        "forced_skill_source",
        "soft_skill_binding",
        "soft_skill_binding_source",
        "soft_skill_binding_requested_capability_id",
    }
)


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
        runtime_replanner: RuntimeReplanner | None = None,
    ) -> None:
        self._storage = storage
        self._capability_registry = capability_registry
        self._instance_registry = instance_registry
        self._scheduler = scheduler
        self._executor = executor
        self._completion_policy = completion_policy
        self._backpressure = backpressure
        self._event_sink = event_sink
        self._runtime_replanner = runtime_replanner or NoopRuntimeReplanner()
        self._event_counter = 0

    _TERMINAL_NODE_STATUSES = {
        NodeStatus.COMPLETED,
        NodeStatus.FAILED,
        NodeStatus.CANCELLED,
        NodeStatus.BLOCKED_BY_CANCELLATION,
        NodeStatus.ORPHANED,
    }

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
        event = self._ensure_event_created_at(event)
        await self._storage.append_event(event)
        if self._event_sink is not None:
            await self._event_sink.publish(event)

    def _ensure_event_created_at(self, event: EventRecord) -> EventRecord:
        if event.created_at is not None:
            return event
        return replace(event, created_at=self._utcnow_naive())

    @staticmethod
    def _node_activity_payload(node_plan: WorkflowNodePlan, *, instance_id: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"capability_id": node_plan.capability_id}
        if instance_id is not None:
            payload["instance_id"] = instance_id
        for key in ("skill_name", "forced_skill_name"):
            skill_name = node_plan.metadata.get(key)
            if isinstance(skill_name, str) and skill_name.strip():
                payload["skill_name"] = skill_name.strip()
                break
        return payload

    async def _initialize_task(self, request: OrchestrationRequest, plan: WorkflowPlan) -> Task:
        now = self._utcnow_naive()
        existing_task = await self._storage.get_task(request.task_id)
        planned_root_node_id = next((node.node_id for node in plan.nodes if not node.depends_on), None)
        root_node_id = (
            existing_task.root_node_id
            if (
                existing_task is not None
                and request.metadata.get("resume_interrupted_node_id")
                and existing_task.root_node_id
            )
            else planned_root_node_id
        )
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
            else:
                await self._storage.save_task_node(
                    replace(
                        existing_nodes[node.node_id],
                        status=NodeStatus.PENDING,
                        assigned_instance_id=None,
                        output_refs=(),
                        started_at=None,
                        finished_at=None,
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

    async def _apply_runtime_replan(
        self,
        *,
        request: OrchestrationRequest,
        current_plan: WorkflowPlan,
        decision: RuntimeReplanDecision,
        current_nodes: dict[str, TaskNode],
        replan_count: int,
        dynamic_node_count: int,
    ) -> tuple[WorkflowPlan, int] | None:
        revised_plan = decision.plan
        try:
            WorkflowPlanValidator(self._capability_registry, public_only=False).validate(revised_plan)
            for node in revised_plan.nodes:
                self._capability_registry.require(node.capability_id)
            current_plan_by_id = {node.node_id: node for node in current_plan.nodes}
            current_nodes_by_id = {
                **{
                    node.node_id: node
                    for node in await self._storage.list_task_nodes_for_task(current_plan.task_id)
                },
                **current_nodes,
            }
            for node in revised_plan.nodes:
                previous = current_plan_by_id.get(node.node_id)
                stored = current_nodes_by_id.get(node.node_id)
                if (
                    previous is not None
                    and stored is not None
                    and (previous.capability_id != node.capability_id or previous.depends_on != node.depends_on)
                ):
                    raise ValueError("Runtime replan must replace existing node ids instead of mutating capability or dependencies.")
        except Exception as exc:
            await self._record_event(
                self._make_event(
                    task_id=request.task_id,
                    conversation_id=request.conversation_id,
                    event_type="task.replan_rejected",
                    payload={
                        "reason": "invalid_runtime_plan",
                        "decision_reason": decision.reason,
                        "error_type": type(exc).__name__,
                    },
                    visibility=EventVisibility.FRONTEND,
                )
            )
            return None

        previous_plan_node_ids = {node.node_id for node in current_plan.nodes}
        revised_node_ids = {node.node_id for node in revised_plan.nodes}
        added_node_ids = revised_node_ids - previous_plan_node_ids
        next_dynamic_node_count = dynamic_node_count + len(added_node_ids)
        if next_dynamic_node_count > current_plan.max_dynamic_nodes:
            await self._record_event(
                self._make_event(
                    task_id=request.task_id,
                    conversation_id=request.conversation_id,
                    event_type="task.replan_rejected",
                    payload={
                        "reason": "dynamic_node_budget_exceeded",
                        "requested_added_node_count": len(added_node_ids),
                        "dynamic_node_count": dynamic_node_count,
                        "max_dynamic_nodes": current_plan.max_dynamic_nodes,
                        "decision_reason": decision.reason,
                    },
                    visibility=EventVisibility.FRONTEND,
                )
            )
            return None

        await self._record_event(
            self._make_event(
                task_id=request.task_id,
                conversation_id=request.conversation_id,
                event_type="task.replan_started",
                payload={
                    "replan_index": replan_count + 1,
                    "reason": decision.reason,
                    "metadata": self._json_safe_mapping(decision.metadata),
                },
                visibility=EventVisibility.FRONTEND,
            )
        )

        latest_nodes = {
            node.node_id: node
            for node in await self._storage.list_task_nodes_for_task(revised_plan.task_id)
        }
        latest_nodes.update(current_nodes)
        for node_plan in revised_plan.nodes:
            existing = latest_nodes.get(node_plan.node_id)
            if existing is None:
                saved = await self._storage.save_task_node(
                    TaskNode(
                        node_id=node_plan.node_id,
                        task_id=revised_plan.task_id,
                        capability_id=node_plan.capability_id,
                        status=NodeStatus.PENDING,
                        criticality=node_plan.criticality,
                        retry_policy=dict(node_plan.retry_policy),
                        timeout_policy=dict(node_plan.timeout_policy),
                        resource_class=node_plan.resource_class,
                    )
                )
                latest_nodes[node_plan.node_id] = saved
            elif existing.status not in self._TERMINAL_NODE_STATUSES:
                latest_nodes[node_plan.node_id] = await self._storage.save_task_node(
                    replace(
                        existing,
                        capability_id=node_plan.capability_id,
                        criticality=node_plan.criticality,
                        retry_policy=dict(node_plan.retry_policy),
                        timeout_policy=dict(node_plan.timeout_policy),
                        resource_class=node_plan.resource_class,
                    )
                )

            for dependency in node_plan.depends_on:
                await self._storage.save_task_edge(
                    revised_plan.task_id,
                    TaskEdge(from_node_id=dependency, to_node_id=node_plan.node_id),
                )

        orphaned_node_ids: list[str] = []
        for node_id in previous_plan_node_ids - revised_node_ids:
            node = latest_nodes.get(node_id)
            if node is not None and node.status not in self._TERMINAL_NODE_STATUSES:
                await self._storage.save_task_node(replace(node, status=NodeStatus.ORPHANED))
                orphaned_node_ids.append(node_id)

        await self._record_event(
            self._make_event(
                task_id=request.task_id,
                conversation_id=request.conversation_id,
                event_type="task.graph_updated",
                payload={
                    "replan_index": replan_count + 1,
                    "node_count": len(revised_plan.nodes),
                    "edge_count": sum(len(node.depends_on) for node in revised_plan.nodes),
                    "added_node_ids": tuple(sorted(added_node_ids)),
                    "orphaned_node_ids": tuple(sorted(orphaned_node_ids)),
                    "reason": decision.reason,
                },
                visibility=EventVisibility.FRONTEND,
            )
        )
        await self._record_event(
            self._make_event(
                task_id=request.task_id,
                conversation_id=request.conversation_id,
                event_type="task.replanned",
                payload={
                    "replan_index": replan_count + 1,
                    "reason": decision.reason,
                    "added_node_count": len(added_node_ids),
                    "dynamic_node_count": next_dynamic_node_count,
                    "max_dynamic_nodes": current_plan.max_dynamic_nodes,
                    "metadata": self._json_safe_mapping(decision.metadata),
                },
                visibility=EventVisibility.AUDIT_ONLY,
            )
        )
        return revised_plan, next_dynamic_node_count

    @staticmethod
    def _json_safe_mapping(value: Any) -> dict[str, Any]:
        import json

        if not isinstance(value, dict):
            value = dict(value or {})
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))

    @staticmethod
    def _execution_metadata(
        request_metadata: Any,
        node_metadata: Any,
    ) -> dict[str, Any]:
        request_values = dict(request_metadata or {})
        node_values = dict(node_metadata or {})
        for key in _SYSTEM_NODE_METADATA_KEYS:
            if key not in node_values:
                request_values.pop(key, None)
        request_values.update(node_values)
        return request_values

    async def _build_runtime_replan_decision(
        self,
        *,
        request: OrchestrationRequest,
        plan: WorkflowPlan,
        nodes: dict[str, TaskNode],
        node_outputs: dict[str, dict[str, Any]],
        completion: CompletionStatus,
        replan_count: int,
        dynamic_node_count: int,
        unresolved_interrupt: bool,
    ) -> RuntimeReplanDecision | None:
        try:
            decision = self._runtime_replanner.build_replan(
                RuntimeReplanContext(
                    request=request,
                    plan=plan,
                    nodes=nodes,
                    node_outputs=node_outputs,
                    completion_status=completion,
                    replan_count=replan_count,
                    dynamic_node_count=dynamic_node_count,
                    unresolved_interrupt=unresolved_interrupt,
                )
            )
            if inspect.isawaitable(decision):
                decision = await decision
            return decision
        except Exception as exc:
            await self._record_event(
                self._make_event(
                    task_id=request.task_id,
                    conversation_id=request.conversation_id,
                    event_type="task.replan_rejected",
                    payload={
                        "reason": "runtime_replanner_error",
                        "error_type": type(exc).__name__,
                    },
                    visibility=EventVisibility.FRONTEND,
                )
            )
            return None

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
                payload=self._node_activity_payload(node_plan, instance_id=instance.instance_id),
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
                metadata=self._execution_metadata(request.metadata, node_plan.metadata),
            )
        )

        latest_task = await self._storage.get_task(request.task_id)
        latest_node = await self._storage.get_task_node(task_node.node_id) or running
        if latest_task is not None and (
            latest_task.status in {TaskStatus.CANCELLING, TaskStatus.CANCELLED}
            or latest_task.cancel_requested_at is not None
        ):
            diagnostic = result.output_payload.get("stream_diagnostic") if isinstance(result.output_payload, dict) else None
            payload = {
                "capability_id": node_plan.capability_id,
                "partial_output_discarded": True,
            }
            if isinstance(diagnostic, dict):
                payload.update({key: value for key, value in diagnostic.items() if key != "delta"})
            await self._record_event(
                self._make_event(
                    task_id=request.task_id,
                    conversation_id=request.conversation_id,
                    node_id=task_node.node_id,
                    event_type="task.late_result_discarded",
                    payload=payload,
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
            updated = replace(latest_node, status=NodeStatus.WAITING_FOR_INPUT)
            saved_node = await self._storage.save_task_node(updated)
            interrupt = replace(result.interrupt, created_at=result.interrupt.created_at or now)
            saved_interrupt = await self._storage.save_interrupt(interrupt)
            await self._record_event(
                self._make_event(
                    task_id=request.task_id,
                    conversation_id=request.conversation_id,
                    node_id=task_node.node_id,
                    event_type="node.waiting_for_input",
                    payload={
                        **self._node_activity_payload(node_plan),
                        "reason": saved_interrupt.reason_code,
                        "interrupt_id": saved_interrupt.interrupt_id,
                        "reason_code": saved_interrupt.reason_code,
                    },
                )
            )
            return saved_node, dict(result.output_payload)

        if result.error is not None:
            if result.error.code == "skill_input_missing":
                waiting = replace(
                    latest_node,
                    status=NodeStatus.WAITING_FOR_INPUT,
                    finished_at=now,
                    output_refs=tuple(artifact.artifact_id for artifact in result.artifacts),
                )
                waiting = await self._storage.save_task_node(waiting)
                await self._record_event(
                    self._make_event(
                        task_id=request.task_id,
                        conversation_id=request.conversation_id,
                        node_id=task_node.node_id,
                        event_type="node.waiting_for_input",
                        payload={**self._node_activity_payload(node_plan), "reason": "skill_input_missing"},
                    )
                )
                return waiting, dict(result.output_payload)
            failed = replace(latest_node, status=NodeStatus.FAILED, finished_at=now)
            failed = await self._storage.save_task_node(failed)
            await self._record_event(
                self._make_event(
                    task_id=request.task_id,
                    conversation_id=request.conversation_id,
                    node_id=task_node.node_id,
                    event_type="node.failed",
                    payload={"code": result.error.code, **dict(result.error.metadata)},
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
                payload=self._node_activity_payload(node_plan),
            )
        )
        return completed, dict(result.output_payload)

    async def execute_request(self, request: OrchestrationRequest, plan: WorkflowPlan, *, active_task_count: int) -> OrchestrationRunResult:
        self._backpressure.ensure_can_accept(active_task_count=active_task_count)
        WorkflowPlanValidator(self._capability_registry, public_only=False).validate(plan)
        for node in plan.nodes:
            self._capability_registry.require(node.capability_id)

        existing_task = await self._storage.get_task(request.task_id)
        if existing_task is not None and existing_task.status in {TaskStatus.CANCELLING, TaskStatus.CANCELLED}:
            return OrchestrationRunResult(task=existing_task, nodes=(), completion_status=existing_task.status.value)

        task = await self._initialize_task(request, plan)
        replan_count = 0
        dynamic_node_count = 0
        max_replans = plan.max_replans
        max_dynamic_nodes = plan.max_dynamic_nodes
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
            runtime_replanned = False
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
                    refreshed_task = await self._storage.get_task(task.task_id)
                    if refreshed_task is not None and refreshed_task.status in {TaskStatus.CANCELLING, TaskStatus.CANCELLED}:
                        return OrchestrationRunResult(
                            task=refreshed_task,
                            nodes=tuple(await self._storage.list_task_nodes_for_task(plan.task_id)),
                            completion_status=refreshed_task.status.value,
                        )
                    nodes[node.node_id] = updated
                    node_outputs[node.node_id] = output_payload
                    progress_made = True
                    if updated.status == NodeStatus.COMPLETED:
                        completed_ids.add(updated.node_id)
                        decision = await self._build_runtime_replan_decision(
                            request=request,
                            plan=plan,
                            nodes=nodes,
                            node_outputs=node_outputs,
                            completion=CompletionStatus.RUNNING,
                            replan_count=replan_count,
                            dynamic_node_count=dynamic_node_count,
                            unresolved_interrupt=False,
                        )
                        if decision is not None:
                            if replan_count >= max_replans:
                                await self._record_event(
                                    self._make_event(
                                        task_id=task.task_id,
                                        conversation_id=task.conversation_id,
                                        event_type="task.replan_rejected",
                                        payload={
                                            "reason": "replan_budget_exhausted",
                                            "replan_count": replan_count,
                                            "max_replans": max_replans,
                                            "decision_reason": decision.reason,
                                        },
                                    )
                                )
                                task = await self._storage.save_task(replace(task, status=TaskStatus.FAILED, updated_at=self._utcnow_naive()))
                                await self._record_event(
                                    self._make_event(task_id=task.task_id, conversation_id=task.conversation_id, event_type="task.failed")
                                )
                                return OrchestrationRunResult(
                                    task=task,
                                    nodes=tuple(await self._storage.list_task_nodes_for_task(plan.task_id)),
                                    completion_status=CompletionStatus.FAILED.value,
                                )
                            applied = await self._apply_runtime_replan(
                                request=request,
                                current_plan=plan,
                                decision=decision,
                                current_nodes=nodes,
                                replan_count=replan_count,
                                dynamic_node_count=dynamic_node_count,
                            )
                            if applied is None:
                                task = await self._storage.save_task(replace(task, status=TaskStatus.FAILED, updated_at=self._utcnow_naive()))
                                await self._record_event(
                                    self._make_event(task_id=task.task_id, conversation_id=task.conversation_id, event_type="task.failed")
                                )
                                return OrchestrationRunResult(
                                    task=task,
                                    nodes=tuple(await self._storage.list_task_nodes_for_task(plan.task_id)),
                                    completion_status=CompletionStatus.FAILED.value,
                                )
                            plan, dynamic_node_count = applied
                            replan_count += 1
                            max_replans = min(max_replans, plan.max_replans)
                            max_dynamic_nodes = min(max_dynamic_nodes, plan.max_dynamic_nodes)
                            task = await self._storage.save_task(replace(task, status=TaskStatus.RUNNING, updated_at=self._utcnow_naive()))
                            runtime_replanned = True
                            break

            if runtime_replanned:
                continue

            cancellation_result = await self._cancellation_result_if_requested(task.task_id, plan)
            if cancellation_result is not None:
                return cancellation_result

            unresolved_interrupt = any(interrupt.status == "open" for interrupt in await self._storage.list_interrupts_for_task(plan.task_id))
            completion = self._completion_policy.evaluate(
                plan,
                {node_id: node.status for node_id, node in nodes.items()},
                replan_count=replan_count,
                unresolved_interrupt=unresolved_interrupt,
            )

            if completion == CompletionStatus.COMPLETED:
                decision = await self._build_runtime_replan_decision(
                    request=request,
                    plan=plan,
                    nodes=nodes,
                    node_outputs=node_outputs,
                    completion=completion,
                    replan_count=replan_count,
                    dynamic_node_count=dynamic_node_count,
                    unresolved_interrupt=unresolved_interrupt,
                )
                if decision is not None:
                    if replan_count >= max_replans:
                        await self._record_event(
                            self._make_event(
                                task_id=task.task_id,
                                conversation_id=task.conversation_id,
                                event_type="task.replan_rejected",
                                payload={
                                    "reason": "replan_budget_exhausted",
                                    "replan_count": replan_count,
                                    "max_replans": max_replans,
                                    "decision_reason": decision.reason,
                                },
                            )
                        )
                        task = await self._storage.save_task(replace(task, status=TaskStatus.FAILED, updated_at=self._utcnow_naive()))
                        await self._record_event(
                            self._make_event(task_id=task.task_id, conversation_id=task.conversation_id, event_type="task.failed")
                        )
                        return OrchestrationRunResult(task=task, nodes=tuple(nodes.values()), completion_status=CompletionStatus.FAILED.value)

                    applied = await self._apply_runtime_replan(
                        request=request,
                        current_plan=plan,
                        decision=decision,
                        current_nodes=nodes,
                        replan_count=replan_count,
                        dynamic_node_count=dynamic_node_count,
                    )
                    if applied is None:
                        task = await self._storage.save_task(replace(task, status=TaskStatus.FAILED, updated_at=self._utcnow_naive()))
                        await self._record_event(
                            self._make_event(task_id=task.task_id, conversation_id=task.conversation_id, event_type="task.failed")
                        )
                        return OrchestrationRunResult(task=task, nodes=tuple(await self._storage.list_task_nodes_for_task(plan.task_id)), completion_status=CompletionStatus.FAILED.value)
                    plan, dynamic_node_count = applied
                    replan_count += 1
                    max_replans = min(max_replans, plan.max_replans)
                    max_dynamic_nodes = min(max_dynamic_nodes, plan.max_dynamic_nodes)
                    task = await self._storage.save_task(replace(task, status=TaskStatus.RUNNING, updated_at=self._utcnow_naive()))
                    continue

                if request.metadata.get("defer_task_completed_until_pending_skill_context_processed") is True:
                    task = await self._storage.save_task(replace(task, status=TaskStatus.RUNNING, updated_at=self._utcnow_naive()))
                else:
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
                decision = None
                if replan_count < max_replans:
                    decision = await self._build_runtime_replan_decision(
                        request=request,
                        plan=plan,
                        nodes=nodes,
                        node_outputs=node_outputs,
                        completion=completion,
                        replan_count=replan_count,
                        dynamic_node_count=dynamic_node_count,
                        unresolved_interrupt=unresolved_interrupt,
                    )
                if decision is not None:
                    applied = await self._apply_runtime_replan(
                        request=request,
                        current_plan=plan,
                        decision=decision,
                        current_nodes=nodes,
                        replan_count=replan_count,
                        dynamic_node_count=dynamic_node_count,
                    )
                    if applied is not None:
                        plan, dynamic_node_count = applied
                        replan_count += 1
                        max_replans = min(max_replans, plan.max_replans)
                        max_dynamic_nodes = min(max_dynamic_nodes, plan.max_dynamic_nodes)
                        task = await self._storage.save_task(replace(task, status=TaskStatus.RUNNING, updated_at=self._utcnow_naive()))
                        continue

                task = await self._storage.save_task(replace(task, status=TaskStatus.FAILED, updated_at=self._utcnow_naive()))
                await self._record_event(
                    self._make_event(
                        task_id=task.task_id,
                        conversation_id=task.conversation_id,
                        event_type="task.replan_available",
                        payload={
                            "replan_count": replan_count,
                            "max_replans": max_replans,
                            "max_dynamic_nodes": max_dynamic_nodes,
                        },
                    )
                )
                await self._record_event(
                    self._make_event(
                        task_id=task.task_id,
                        conversation_id=task.conversation_id,
                        event_type="task.failed",
                        payload={
                            "code": "replan_unavailable",
                            "replan_count": replan_count,
                            "max_replans": max_replans,
                            "max_dynamic_nodes": max_dynamic_nodes,
                        },
                    )
                )
                return OrchestrationRunResult(task=task, nodes=tuple(nodes.values()), completion_status=CompletionStatus.FAILED.value)

            if completion == CompletionStatus.WAITING_FOR_INPUT or not progress_made:
                task = await self._storage.save_task(replace(task, status=TaskStatus.RUNNING, updated_at=self._utcnow_naive()))
                return OrchestrationRunResult(task=task, nodes=tuple(nodes.values()), completion_status=completion.value)

    async def _cancellation_result_if_requested(
        self,
        task_id: str,
        plan: WorkflowPlan,
    ) -> OrchestrationRunResult | None:
        current_task = await self._storage.get_task(task_id)
        if current_task is None:
            return None
        if current_task.status not in {TaskStatus.CANCELLING, TaskStatus.CANCELLED} and current_task.cancel_requested_at is None:
            return None
        return OrchestrationRunResult(
            task=current_task,
            nodes=tuple(await self._storage.list_task_nodes_for_task(plan.task_id)),
            completion_status=current_task.status.value,
        )
