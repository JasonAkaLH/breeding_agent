from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any, Awaitable, Callable

from src.core.contracts import CapabilityExecutionResult, StoragePort
from src.core.enums import EventVisibility, NodeStatus, TaskStatus
from src.core.models import EventRecord, Interrupt, Task, TaskNode
from src.integrations.agent_skills.missing_input_interrupt import slot_collection_event_payload
from src.orchestration.visible_message_history import persist_interrupt_question_message

from .invocation import InvocationRequest


class LegacyDAGInvocationCommitPort:
    """Temporary Phase 2 adapter for the current DAG storage projections."""

    def __init__(
        self,
        *,
        storage: StoragePort,
        make_event: Callable[..., EventRecord],
        record_event: Callable[[EventRecord], Awaitable[None]],
        assert_execution_owned: Callable[[InvocationRequest], Awaitable[None]],
        persist_interrupt_authority: Callable[[Interrupt, datetime], Awaitable[Interrupt]],
    ) -> None:
        self._storage = storage
        self._make_event = make_event
        self._record_event = record_event
        self._assert_owned = assert_execution_owned
        self._persist_interrupt_authority = persist_interrupt_authority

    async def assert_execution_owned(self, request: InvocationRequest) -> None:
        await self._assert_owned(request)

    async def start_node(
        self,
        request: InvocationRequest,
        node: TaskNode,
        *,
        instance_id: str,
        started_at: datetime,
        activity_payload: dict[str, Any],
    ) -> TaskNode:
        running = replace(
            node,
            status=NodeStatus.RUNNING,
            assigned_instance_id=instance_id,
            started_at=started_at,
        )
        running = await self._storage.compare_and_set_task_node(
            running, expected_from_status=node.status
        )
        if running is None:
            raise RuntimeError("mcp_continuation_node_start_conflict")
        await self._lifecycle_event(request, "node.started", activity_payload)
        return running

    async def get_task_snapshot(self, task_id: str) -> Task | None:
        return await self._storage.get_task(task_id)

    async def get_node_snapshot(self, node_id: str) -> TaskNode | None:
        return await self._storage.get_task_node(node_id)

    async def commit_completed(
        self,
        request: InvocationRequest,
        node: TaskNode,
        result: CapabilityExecutionResult,
        *,
        now: datetime,
        activity_payload: dict[str, Any],
    ) -> TaskNode:
        await self._persist_result_side_effects(result)
        completed = await self._storage.save_task_node(
            replace(
                node,
                status=NodeStatus.COMPLETED,
                finished_at=now,
                output_refs=tuple(artifact.artifact_id for artifact in result.artifacts),
            )
        )
        await self._lifecycle_event(request, "node.completed", activity_payload)
        return completed

    async def commit_failed(
        self,
        request: InvocationRequest,
        node: TaskNode,
        result: CapabilityExecutionResult,
        *,
        now: datetime,
        activity_payload: dict[str, Any],
    ) -> TaskNode:
        await self._persist_result_side_effects(result)
        failed = await self._storage.save_task_node(
            replace(node, status=NodeStatus.FAILED, finished_at=now)
        )
        error = result.error
        await self._lifecycle_event(
            request,
            "node.failed",
            {"code": error.code, **dict(error.metadata)} if error is not None else {},
        )
        return failed

    async def commit_waiting_for_input(
        self,
        request: InvocationRequest,
        node: TaskNode,
        result: CapabilityExecutionResult,
        *,
        now: datetime,
        activity_payload: dict[str, Any],
    ) -> TaskNode:
        await self._persist_result_side_effects(result)
        if result.interrupt is not None:
            waiting = await self._storage.save_task_node(
                replace(node, status=NodeStatus.WAITING_FOR_INPUT)
            )
            interrupt = replace(
                result.interrupt,
                created_at=result.interrupt.created_at or now,
            )
            interrupt = await self._persist_interrupt_authority(interrupt, now)
            saved_interrupt = await self._storage.save_interrupt(interrupt)
            await persist_interrupt_question_message(
                self._storage,
                saved_interrupt,
                created_at=saved_interrupt.created_at or now,
            )
            await self._lifecycle_event(
                request,
                "node.waiting_for_input",
                {
                    **activity_payload,
                    "reason": saved_interrupt.reason_code,
                    "interrupt_id": saved_interrupt.interrupt_id,
                    "reason_code": saved_interrupt.reason_code,
                    **slot_collection_event_payload(saved_interrupt.required_fields),
                },
            )
            return waiting
        waiting = await self._storage.save_task_node(
            replace(
                node,
                status=NodeStatus.WAITING_FOR_INPUT,
                finished_at=now,
                output_refs=tuple(artifact.artifact_id for artifact in result.artifacts),
            )
        )
        await self._lifecycle_event(
            request,
            "node.waiting_for_input",
            {**activity_payload, "reason": "skill_input_missing"},
        )
        return waiting

    async def commit_waiting_for_dependency(
        self,
        request: InvocationRequest,
        node: TaskNode,
        result: CapabilityExecutionResult,
        *,
        now: datetime,
        activity_payload: dict[str, Any],
    ) -> TaskNode:
        await self._persist_result_side_effects(result)
        waiting = await self._storage.save_task_node(
            replace(node, status=NodeStatus.WAITING_FOR_DEPENDENCY)
        )
        safe_remote_task_ref = str(
            result.output_payload.get("safe_remote_task_ref") or ""
        ).strip()
        if not safe_remote_task_ref:
            raise RuntimeError("mcp_remote_task_reference_missing")
        conversation = await self._storage.get_conversation(request.conversation_id)
        if conversation is None:
            raise RuntimeError("mcp_remote_task_conversation_missing")
        published = await self._storage.publish_mcp_remote_task_binding(
            conversation.username,
            request.task_id,
            safe_remote_task_ref,
            published_at=now,
            continuation_plan=(
                dict(request.remote_task_continuation_plan)
                if request.remote_task_continuation_plan is not None
                else None
            ),
        )
        if published is None:
            raise RuntimeError("mcp_remote_task_publication_failed")
        await self._lifecycle_event(
            request,
            "node.waiting_for_dependency",
            {
                **activity_payload,
                "reason": "mcp_remote_task_pending",
                "safe_call_ref": result.output_payload.get("safe_call_ref"),
            },
        )
        return waiting

    async def discard_late_result(
        self,
        request: InvocationRequest,
        node: TaskNode,
        result: CapabilityExecutionResult,
        *,
        activity_payload: dict[str, Any],
    ) -> TaskNode:
        diagnostic = (
            result.output_payload.get("stream_diagnostic")
            if isinstance(result.output_payload, dict)
            else None
        )
        payload = {
            "capability_id": request.capability_id,
            "partial_output_discarded": True,
        }
        if isinstance(diagnostic, dict):
            payload.update({key: value for key, value in diagnostic.items() if key != "delta"})
        await self._lifecycle_event(
            request,
            "task.late_result_discarded",
            payload,
            visibility=EventVisibility.AUDIT_ONLY,
        )
        return node

    async def commit_route_rejected(
        self,
        request: InvocationRequest,
        node: TaskNode,
        *,
        rejection_code: str,
        now: datetime,
        activity_payload: dict[str, Any],
    ) -> TaskNode:
        failed = await self._storage.compare_and_set_task_node(
            replace(node, status=NodeStatus.FAILED, finished_at=now),
            expected_from_status=node.status,
        )
        if failed is not None:
            await self._lifecycle_event(
                request,
                "node.failed",
                {"code": rejection_code},
            )
            return failed
        latest_task = await self._storage.get_task(request.task_id)
        latest_node = await self._storage.get_task_node(request.node_id)
        if latest_node is None:
            raise RuntimeError("mcp_selected_route_rejection_node_missing")
        if latest_node.status in {
            NodeStatus.CANCELLED,
            NodeStatus.BLOCKED_BY_CANCELLATION,
        } or (
            latest_task is not None
            and (
                latest_task.cancel_requested_at is not None
                or latest_task.status in {TaskStatus.CANCELLING, TaskStatus.CANCELLED}
            )
        ):
            return latest_node
        raise RuntimeError("mcp_selected_route_rejection_conflict")

    async def _persist_result_side_effects(
        self, result: CapabilityExecutionResult
    ) -> None:
        for artifact in result.artifacts:
            await self._storage.save_artifact(artifact)
        for event in result.events:
            await self._record_event(event)

    async def _lifecycle_event(
        self,
        request: InvocationRequest,
        event_type: str,
        payload: dict[str, Any],
        *,
        visibility: EventVisibility = EventVisibility.FRONTEND,
    ) -> None:
        await self._record_event(
            self._make_event(
                task_id=request.task_id,
                conversation_id=request.conversation_id,
                node_id=request.node_id,
                event_type=event_type,
                payload=payload,
                visibility=visibility,
            )
        )
