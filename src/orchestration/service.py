from __future__ import annotations

import inspect
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Mapping

from src.core.contracts import CapabilityExecutionRequest, EventSink, ExecutorPort, StoragePort
from src.core.enums import EventVisibility, NodeStatus, TaskStatus
from src.core.models import EventRecord, Interrupt, Task, TaskEdge, TaskNode
from src.integrations.mcp.cp7_artifacts import canonical_sha256
from src.integrations.agent_skills.missing_input_interrupt import (
    SLOT_COLLECTION_V2_SCHEMA_VERSION,
    slot_collection_bootstrap_events,
    slot_collection_event_payload,
    slot_collection_from_required_fields,
    slot_collection_model_from_carrier,
    slot_collection_required_fields_ref,
)

from .backpressure import BackpressureGuard
from .completion_policy import CompletionPolicy, CompletionStatus
from .models import OrchestrationRequest, OrchestrationRunResult, WorkflowNodePlan, WorkflowPlan
from .registry import CapabilityRegistry, InstanceRegistry
from .runtime_replanner import NoopRuntimeReplanner, RuntimeReplanContext, RuntimeReplanDecision, RuntimeReplanner
from .scheduler import Scheduler
from .visible_message_history import persist_interrupt_question_message
from .workflow_plan_validator import WorkflowPlanValidator

_SYSTEM_NODE_METADATA_KEYS = frozenset(
    {
        "forced_skill_capability_id",
        "forced_skill_name",
        "forced_skill_source",
        "soft_skill_binding",
        "soft_skill_binding_source",
        "soft_skill_binding_requested_capability_id",
        "mcp_dispatch_server_id",
        "mcp_binding_mode",
        "forced_by_mcp_command",
        "mcp_command",
    }
)
_TASK_AUTHORITY_METADATA_KEYS = frozenset(
    {
        "mcp_execution_mode",
        "mcp_shadow_enabled",
        "mcp_rollout_config_version",
        "mcp_route_reason_code",
        "mcp_rollout_mode",
    }
)


def _serialize_workflow_plan(plan: WorkflowPlan) -> dict[str, Any]:
    return {
        "task_id": plan.task_id,
        "nodes": [
            {
                "node_id": node.node_id,
                "capability_id": node.capability_id,
                "input_payload": dict(node.input_payload),
                "metadata": dict(node.metadata),
                "depends_on": list(node.depends_on),
                "criticality": node.criticality.value,
                "retry_policy": dict(node.retry_policy),
                "timeout_policy": dict(node.timeout_policy),
                "resource_class": node.resource_class,
            }
            for node in plan.nodes
        ],
        "metadata": dict(plan.metadata),
        "max_replans": plan.max_replans,
        "max_dynamic_nodes": plan.max_dynamic_nodes,
    }


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

    async def _persist_v2_slot_collection_for_interrupt(self, interrupt: Interrupt, *, now: datetime) -> Interrupt:
        carrier = slot_collection_from_required_fields(interrupt.required_fields)
        if carrier is None:
            return interrupt
        if int(carrier.get("schema_version") or 0) != SLOT_COLLECTION_V2_SCHEMA_VERSION:
            return interrupt
        collection = slot_collection_model_from_carrier(carrier, now=now)
        if not collection.collection_id:
            return interrupt
        existing = await self._storage.get_slot_collection(collection.collection_id)
        if existing is None:
            await self._storage.save_slot_collection(collection)
            for event in slot_collection_bootstrap_events(collection, now=now):
                await self._storage.append_slot_event(event)
        else:
            prompt_key = f"slot:{existing.collection_id}:prompt:{collection.round}"
            if await self._storage.get_slot_event_by_idempotency_key(existing.collection_id, prompt_key) is None:
                merged = replace(
                    existing,
                    status=collection.status or existing.status,
                    round=max(existing.round, collection.round),
                    selected_schema_id=collection.selected_schema_id or existing.selected_schema_id,
                    selected_entrypoint=collection.selected_entrypoint or existing.selected_entrypoint,
                    schema_digest=collection.schema_digest or existing.schema_digest,
                    schema_snapshot=dict(collection.schema_snapshot or existing.schema_snapshot),
                    slots=dict(collection.slots or existing.slots),
                    resolved=dict(collection.resolved or existing.resolved),
                    missing=tuple(collection.missing),
                    invalid=tuple(collection.invalid),
                    last_question=collection.last_question or existing.last_question,
                    revision=existing.revision + 1,
                    created_at=existing.created_at,
                    updated_at=now,
                )
                prompt_event = slot_collection_bootstrap_events(collection, now=now)[-1]
                prompt_event = replace(
                    prompt_event,
                    revision=merged.revision,
                    idempotency_key=prompt_key,
                    payload={
                        **dict(prompt_event.payload),
                        "replaces_revision": existing.revision,
                    },
                )
                collection = await self._storage.apply_slot_transition(
                    existing.collection_id,
                    existing.revision,
                    merged,
                    prompt_event,
                    idempotency_key=prompt_key,
                ) or await self._storage.get_slot_collection(existing.collection_id) or existing
            else:
                collection = existing
        return replace(interrupt, required_fields=slot_collection_required_fields_ref(collection))

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
        resuming_nodes: list[tuple[TaskNode, WorkflowNodePlan]] = []
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
                existing_node = existing_nodes[node.node_id]
                is_resume_node = (
                    request.metadata.get("resume_interrupted_node_id") == node.node_id
                    and existing_node.status in {NodeStatus.READY_TO_RESUME, NodeStatus.RESUMING}
                )
                resumed_status = NodeStatus.RESUMING if is_resume_node else NodeStatus.PENDING
                saved_node = await self._storage.save_task_node(
                    replace(
                        existing_node,
                        status=resumed_status,
                        assigned_instance_id=None,
                        output_refs=(),
                        started_at=None,
                        finished_at=None,
                    )
                )
                if is_resume_node and existing_node.status == NodeStatus.READY_TO_RESUME:
                    resuming_nodes.append((saved_node, node))
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
        for resumed_node, node_plan in resuming_nodes:
            await self._record_event(
                self._make_event(
                    task_id=plan.task_id,
                    conversation_id=request.conversation_id,
                    node_id=resumed_node.node_id,
                    event_type="node.resuming",
                    payload={
                        **self._node_activity_payload(node_plan),
                        "status": str(resumed_node.status),
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
        for node_id in sorted(previous_plan_node_ids - revised_node_ids):
            node = latest_nodes.get(node_id)
            if node is not None and node.status not in self._TERMINAL_NODE_STATUSES:
                orphaned_node = await self._storage.save_task_node(replace(node, status=NodeStatus.ORPHANED))
                orphaned_node_ids.append(node_id)
                node_plan = current_plan_by_id.get(node_id)
                activity_payload = (
                    self._node_activity_payload(node_plan)
                    if node_plan is not None
                    else {"capability_id": orphaned_node.capability_id}
                )
                await self._record_event(
                    self._make_event(
                        task_id=request.task_id,
                        conversation_id=request.conversation_id,
                        node_id=node_id,
                        event_type="node.orphaned",
                        payload={
                            **activity_payload,
                            "status": str(orphaned_node.status),
                            "reason": decision.reason,
                            "replan_index": replan_count + 1,
                        },
                        visibility=EventVisibility.FRONTEND,
                    )
                )

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
        for key in _TASK_AUTHORITY_METADATA_KEYS:
            node_values.pop(key, None)
        for key in _SYSTEM_NODE_METADATA_KEYS:
            if key not in node_values:
                request_values.pop(key, None)
        request_values.update(node_values)
        return request_values

    @staticmethod
    def _task_authoritative_metadata(
        metadata: dict[str, Any],
        task: Task | None,
    ) -> dict[str, Any]:
        values = dict(metadata)
        for key in _TASK_AUTHORITY_METADATA_KEYS:
            values.pop(key, None)
        if task is None:
            return values
        assignment = (
            task.mcp_execution_mode,
            task.mcp_shadow_enabled,
            task.mcp_rollout_config_version,
            task.mcp_route_reason_code,
            task.mcp_rollout_mode,
        )
        if all(value is None for value in assignment):
            return values
        if any(value is None for value in assignment):
            raise ValueError("mcp_task_route_assignment_corrupt")
        values.update(
            {
                "mcp_execution_mode": task.mcp_execution_mode,
                "mcp_shadow_enabled": task.mcp_shadow_enabled,
                "mcp_rollout_config_version": task.mcp_rollout_config_version,
                "mcp_route_reason_code": task.mcp_route_reason_code,
                "mcp_rollout_mode": task.mcp_rollout_mode,
            }
        )
        return values

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
        await self._assert_mcp_continuation_execution_owned(request)
        instance = self._scheduler.select_instance(node_plan.capability_id)
        running = replace(
            task_node,
            status=NodeStatus.RUNNING,
            assigned_instance_id=instance.instance_id,
            started_at=task_node.started_at or self._utcnow_naive(),
        )
        running = await self._storage.compare_and_set_task_node(
            running, expected_from_status=task_node.status
        )
        if running is None:
            raise RuntimeError("mcp_continuation_node_start_conflict")
        await self._record_event(
            self._make_event(
                task_id=request.task_id,
                conversation_id=request.conversation_id,
                node_id=task_node.node_id,
                event_type="node.started",
                payload=self._node_activity_payload(node_plan, instance_id=instance.instance_id),
            )
        )

        task_snapshot = await self._storage.get_task(request.task_id)
        execution_metadata = self._task_authoritative_metadata(
            self._execution_metadata(request.metadata, node_plan.metadata),
            task_snapshot,
        )
        result = await self._executor.execute(
            CapabilityExecutionRequest(
                capability_id=node_plan.capability_id,
                conversation_id=request.conversation_id,
                task_id=request.task_id,
                node_id=task_node.node_id,
                input_payload=dict(node_plan.input_payload),
                dependency_outputs={dependency: dict(dependency_outputs.get(dependency, {})) for dependency in node_plan.depends_on},
                metadata=execution_metadata,
            )
        )

        await self._assert_mcp_continuation_execution_owned(request)

        latest_task = await self._storage.get_task(request.task_id)
        latest_node = await self._storage.get_task_node(task_node.node_id) or running
        if latest_task is not None and (
            latest_task.status != TaskStatus.RUNNING
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
            interrupt = await self._persist_v2_slot_collection_for_interrupt(interrupt, now=now)
            saved_interrupt = await self._storage.save_interrupt(interrupt)
            await persist_interrupt_question_message(self._storage, saved_interrupt, created_at=saved_interrupt.created_at or now)
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
                        **slot_collection_event_payload(saved_interrupt.required_fields),
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

        if (
            node_plan.capability_id == "mcp.dispatch"
            and result.output_payload.get("mcp_status") == "remote_task_created"
        ):
            waiting = replace(latest_node, status=NodeStatus.WAITING_FOR_DEPENDENCY)
            waiting = await self._storage.save_task_node(waiting)
            safe_remote_task_ref = str(
                result.output_payload.get("safe_remote_task_ref") or ""
            ).strip()
            if not safe_remote_task_ref:
                raise RuntimeError("mcp_remote_task_reference_missing")
            conversation = await self._storage.get_conversation(
                request.conversation_id
            )
            if conversation is None:
                raise RuntimeError("mcp_remote_task_conversation_missing")
            published = await self._storage.publish_mcp_remote_task_binding(
                conversation.username,
                request.task_id,
                safe_remote_task_ref,
                published_at=now,
                continuation_plan=(
                    request.metadata.get("mcp_remote_task_continuation_plan")
                    if isinstance(
                        request.metadata.get("mcp_remote_task_continuation_plan"), dict
                    )
                    else None
                ),
            )
            if published is None:
                raise RuntimeError("mcp_remote_task_publication_failed")
            await self._record_event(
                self._make_event(
                    task_id=request.task_id,
                    conversation_id=request.conversation_id,
                    node_id=task_node.node_id,
                    event_type="node.waiting_for_dependency",
                    payload={
                        **self._node_activity_payload(node_plan),
                        "reason": "mcp_remote_task_pending",
                        "safe_call_ref": result.output_payload.get("safe_call_ref"),
                    },
                )
            )
            return waiting, dict(result.output_payload)

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

    async def resume_persisted_mcp_dispatch_node(
        self,
        request: OrchestrationRequest,
        envelope: Mapping[str, Any],
        *,
        expected_envelope_sha256: str,
    ) -> tuple[TaskNode, dict[str, Any]]:
        if canonical_sha256(dict(envelope)) != expected_envelope_sha256:
            raise RuntimeError("mcp_dispatch_resume_envelope_digest_mismatch")
        task = await self._storage.get_task(request.task_id)
        node = await self._storage.get_task_node(str(envelope.get("node_id") or ""))
        if (
            task is None
            or node is None
            or task.status != TaskStatus.RUNNING
            or task.task_id != envelope.get("task_id")
            or task.root_message_id != envelope.get("root_message_id")
            or task.mcp_execution_mode != "user_scoped"
            or task.mcp_route_reason_code != "enforce_selected"
            or node.task_id != task.task_id
            or node.capability_id != "mcp.dispatch"
        ):
            raise RuntimeError("mcp_dispatch_resume_authority_mismatch")
        assignment = envelope.get("task_assignment")
        snapshot = envelope.get("node_snapshot")
        if not isinstance(assignment, Mapping) or not isinstance(snapshot, Mapping):
            raise RuntimeError("mcp_dispatch_resume_snapshot_missing")
        actual_assignment = {
            "mcp_execution_mode": task.mcp_execution_mode,
            "mcp_route_reason_code": task.mcp_route_reason_code,
            "mcp_rollout_config_version": task.mcp_rollout_config_version,
            "mcp_rollout_mode": task.mcp_rollout_mode,
            "mcp_shadow_enabled": task.mcp_shadow_enabled,
        }
        actual_node = {
            "capability_id": node.capability_id,
            "criticality": str(node.criticality),
            "dependency_type": str(node.dependency_type),
            "input_refs": list(node.input_refs),
            "resource_class": node.resource_class,
            "retry_policy": dict(node.retry_policy),
            "timeout_policy": dict(node.timeout_policy),
        }
        edges = await self._storage.list_task_edges(task.task_id)
        actual_edges = [
            {
                "condition": edge.condition,
                "edge_type": str(edge.edge_type),
                "from_node_id": edge.from_node_id,
                "to_node_id": edge.to_node_id,
            }
            for edge in edges
        ]
        attachments = await self._storage.list_task_input_attachments_for_task(
            task.task_id
        )
        if (
            dict(assignment) != actual_assignment
            or dict(snapshot) != actual_node
            or envelope.get("edge_snapshot") != actual_edges
            or envelope.get("input_attachment_ids")
            != sorted(item.attachment_id for item in attachments)
        ):
            raise RuntimeError("mcp_dispatch_resume_snapshot_drift")
        node_plan = WorkflowNodePlan(
            node_id=node.node_id,
            capability_id=node.capability_id,
            input_payload=dict(envelope.get("input_payload") or {}),
            metadata=dict(envelope.get("metadata") or {}),
            depends_on=tuple(
                edge.from_node_id for edge in edges if edge.to_node_id == node.node_id
            ),
            criticality=node.criticality,
            retry_policy=dict(node.retry_policy),
            timeout_policy=dict(node.timeout_policy),
            resource_class=node.resource_class,
        )
        dependency_outputs = {
            str(key): dict(value)
            for key, value in dict(envelope.get("dependency_outputs") or {}).items()
        }
        return await self._execute_node(
            request, node_plan, node, dependency_outputs=dependency_outputs
        )

    async def _assert_mcp_continuation_execution_owned(
        self, request: OrchestrationRequest
    ) -> None:
        if "mcp_remote_task_continuation_id" not in request.metadata:
            return
        outbox_id = request.metadata.get("mcp_remote_task_continuation_id")
        expected_token = request.metadata.get(
            "mcp_remote_task_continuation_claim_token"
        )
        if not isinstance(expected_token, str) or not expected_token:
            raise RuntimeError("mcp_continuation_claim_token_missing")
        if not isinstance(outbox_id, str):
            raise RuntimeError("mcp_continuation_execution_lease_lost")
        outbox = await self._storage.get_mcp_remote_task_outbox(outbox_id)
        now = self._utcnow_naive()
        if (
            outbox is None
            or outbox.continuation_status != "running"
            or outbox.continuation_claim_token != expected_token
            or outbox.continuation_lease_expires_at is None
            or outbox.continuation_lease_expires_at <= now
        ):
            raise RuntimeError("mcp_continuation_execution_lease_lost")
        task = await self._storage.get_task(request.task_id)
        if task is None or task.status != TaskStatus.RUNNING:
            raise RuntimeError("mcp_continuation_task_not_running")

    async def execute_request(self, request: OrchestrationRequest, plan: WorkflowPlan, *, active_task_count: int) -> OrchestrationRunResult:
        self._backpressure.ensure_can_accept(active_task_count=active_task_count)
        WorkflowPlanValidator(self._capability_registry, public_only=False).validate(plan)
        for node in plan.nodes:
            self._capability_registry.require(node.capability_id)
        is_mcp_continuation = "mcp_remote_task_continuation_id" in request.metadata
        if not is_mcp_continuation:
            request = replace(
                request,
                metadata={
                    **request.metadata,
                    "mcp_remote_task_continuation_plan": _serialize_workflow_plan(plan),
                },
            )

        existing_task = await self._storage.get_task(request.task_id)
        if existing_task is not None and existing_task.status in {TaskStatus.CANCELLING, TaskStatus.CANCELLED}:
            return OrchestrationRunResult(task=existing_task, nodes=(), completion_status=existing_task.status.value)

        if is_mcp_continuation:
            await self._assert_mcp_continuation_execution_owned(request)
            task = await self._resume_remote_task_continuation(request, plan)
        else:
            task = await self._initialize_task(request, plan)
        replan_count = 0
        dynamic_node_count = 0
        max_replans = plan.max_replans
        max_dynamic_nodes = plan.max_dynamic_nodes
        node_outputs: dict[str, dict[str, Any]] = {}
        continuation_result = request.metadata.get("mcp_remote_task_result")
        continuation_source_node_id = request.metadata.get(
            "mcp_remote_task_source_node_id"
        )
        if (
            isinstance(continuation_source_node_id, str)
            and isinstance(continuation_result, dict)
        ):
            node_outputs[continuation_source_node_id] = dict(continuation_result)

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
                    request = replace(
                        request,
                        metadata={
                            **request.metadata,
                            "mcp_remote_task_continuation_plan": _serialize_workflow_plan(
                                plan
                            ),
                        },
                    )
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
                    if (
                        updated.status == NodeStatus.WAITING_FOR_DEPENDENCY
                        and output_payload.get("mcp_status")
                        == "remote_task_created"
                    ):
                        task = await self._storage.save_task(
                            replace(
                                task,
                                status=TaskStatus.RUNNING,
                                updated_at=self._utcnow_naive(),
                            )
                        )
                        return OrchestrationRunResult(
                            task=task,
                            nodes=tuple(nodes.values()),
                            completion_status=CompletionStatus.RUNNING.value,
                        )
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

    async def _resume_remote_task_continuation(
        self, request: OrchestrationRequest, plan: WorkflowPlan
    ) -> Task:
        """Resume a persisted graph without resetting its completed MCP node."""

        task = await self._storage.get_task(request.task_id)
        if task is None or task.status != TaskStatus.RUNNING:
            raise RuntimeError("mcp_continuation_task_not_running")
        existing_nodes = {
            node.node_id: node
            for node in await self._storage.list_task_nodes_for_task(plan.task_id)
        }
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
        return task

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
