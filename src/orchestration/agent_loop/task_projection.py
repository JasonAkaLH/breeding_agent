from __future__ import annotations

import hashlib
import inspect
import json
from dataclasses import replace
from datetime import datetime
from typing import Any, Awaitable, Callable, Mapping, Protocol

from src.core.contracts import (
    CapabilityExecutionResult,
    ConversationStoragePort,
    InterruptStoragePort,
    MCPRemoteTaskStoragePort,
    SlotStoragePort,
    TaskStoragePort,
)
from src.core.enums import EventVisibility, NodeStatus, TaskStatus
from src.core.models import EventRecord, Interrupt, Task, TaskNode
from src.integrations.agent_skills.missing_input_interrupt import (
    SLOT_COLLECTION_V2_SCHEMA_VERSION,
    slot_collection_event_payload,
    slot_collection_bootstrap_events,
    slot_collection_from_required_fields,
    slot_collection_model_from_carrier,
    slot_collection_required_fields_ref,
)
from src.orchestration.visible_message_history import (
    persist_interrupt_question_message,
)

from .invocation import InvocationRequest
from .continuation import AgentContinuationLocatorService, AgentResumeKind
from .models import AgentItemKind, AgentRunStatus, AgentStorageConflict
from .repository import AgentRunRepository


InterruptBinder = Callable[
    [InvocationRequest, Interrupt], Interrupt | Awaitable[Interrupt]
]


class AgentTaskProjectionStoragePort(
    TaskStoragePort,
    InterruptStoragePort,
    ConversationStoragePort,
    MCPRemoteTaskStoragePort,
    SlotStoragePort,
    Protocol,
):
    """Persistence surface used by Agent invocation task projections."""


class AgentTaskInvocationCommitPort:
    """Project one Agent capability call into TaskNode/Event/Interrupt state."""

    def __init__(
        self,
        *,
        storage: AgentTaskProjectionStoragePort,
        runs: AgentRunRepository,
        make_event: Callable[..., EventRecord],
        record_event: Callable[[EventRecord], Awaitable[None]],
        persist_interrupt_authority: Callable[
            [Interrupt, datetime], Awaitable[Interrupt]
        ] | None = None,
        bind_interrupt: InterruptBinder | None = None,
    ) -> None:
        self._storage = storage
        self._runs = runs
        self._make_event = make_event
        self._record_event = record_event
        self._persist_interrupt_authority = persist_interrupt_authority
        self._bind_interrupt = bind_interrupt
        self._continuation_locators: dict[str, dict[str, Any]] = {}

    async def assert_execution_owned(self, request: InvocationRequest) -> None:
        if request.run_id is None or request.call_item_id is None:
            raise AgentStorageConflict("agent_invocation_identity_missing")
        run = await self._runs.get_run(request.run_id)
        task = await self._storage.get_task(request.task_id)
        if (
            run is None
            or run.task_id != request.task_id
            or run.conversation_id != request.conversation_id
            or (
                run.status is not AgentRunStatus.RUNNING
                and not (
                    run.status in {
                        AgentRunStatus.WAITING_FOR_INPUT,
                        AgentRunStatus.WAITING_FOR_DEPENDENCY,
                    }
                    and request.call_item_id in run.waiting_call_item_ids
                )
            )
            or run.claim_token != request.expected_claim_token
            or task is None
            or task.status != TaskStatus.RUNNING
            or task.cancel_requested_at is not None
        ):
            raise AgentStorageConflict("agent_invocation_not_owned")

    async def start_node(
        self,
        request: InvocationRequest,
        node: TaskNode,
        *,
        instance_id: str,
        started_at: datetime,
        activity_payload: dict[str, Any],
    ) -> TaskNode:
        running = await self._storage.compare_and_set_task_node(
            replace(
                node,
                status=NodeStatus.RUNNING,
                assigned_instance_id=instance_id,
                started_at=started_at,
            ),
            expected_from_status=node.status,
        )
        if running is None:
            raise AgentStorageConflict("agent_invocation_node_start_conflict")
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
        await self._persist_events(result)
        completed = await self._storage.save_task_node(
            replace(node, status=NodeStatus.COMPLETED, finished_at=now)
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
        await self._persist_events(result)
        failed = await self._storage.save_task_node(
            replace(node, status=NodeStatus.FAILED, finished_at=now)
        )
        error = result.error
        await self._lifecycle_event(
            request,
            "node.failed",
            {"code": error.code} if error is not None else {},
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
        await self._persist_events(result)
        waiting = await self._storage.save_task_node(
            replace(node, status=NodeStatus.WAITING_FOR_INPUT)
        )
        if result.interrupt is not None:
            interrupt = replace(
                result.interrupt,
                created_at=result.interrupt.created_at or now,
            )
            if self._bind_interrupt is not None:
                bound = self._bind_interrupt(request, interrupt)
                interrupt = await bound if inspect.isawaitable(bound) else bound
            interrupt = await self._bind_agent_continuation(request, interrupt)
            interrupt = (
                await self._persist_interrupt_authority(interrupt, now)
                if self._persist_interrupt_authority is not None
                else await persist_agent_slot_interrupt_authority(
                    self._storage,
                    interrupt,
                    now=now,
                )
            )
            saved = await self._storage.save_interrupt(interrupt)
            await persist_interrupt_question_message(
                self._storage,
                saved,
                created_at=saved.created_at or now,
            )
            await self._lifecycle_event(
                request,
                "node.waiting_for_input",
                {
                    **activity_payload,
                    "interrupt_id": saved.interrupt_id,
                    "reason": saved.reason_code,
                    "reason_code": saved.reason_code,
                    **slot_collection_event_payload(saved.required_fields),
                },
            )
            return waiting
        await self._lifecycle_event(
            request,
            "node.waiting_for_input",
            {**activity_payload, "reason": "skill_input_missing"},
        )
        return waiting

    def continuation_locator_for_call(
        self, call_item_id: str
    ) -> Mapping[str, Any] | None:
        value = self._continuation_locators.get(call_item_id)
        return None if value is None else dict(value)

    async def _bind_agent_continuation(
        self,
        request: InvocationRequest,
        interrupt: Interrupt,
    ) -> Interrupt:
        authority_digest = hashlib.sha256(
            json.dumps(
                {
                    "interrupt_id": interrupt.interrupt_id,
                    "node_id": interrupt.node_id,
                    "reason_code": interrupt.reason_code,
                    "task_id": interrupt.task_id,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        safe_locator = await self._build_continuation_locator(
            request,
            resume_kind=_resume_kind(interrupt),
            authority_digest=authority_digest,
        )
        return replace(
            interrupt,
            required_fields={
                **dict(interrupt.required_fields),
                "_agent_continuation": safe_locator,
            },
        )

    async def _build_continuation_locator(
        self,
        request: InvocationRequest,
        *,
        resume_kind: AgentResumeKind,
        authority_digest: str,
    ) -> dict[str, Any]:
        if request.run_id is None or request.call_item_id is None:
            raise AgentStorageConflict("agent_continuation_identity_missing")
        run = await self._runs.get_run(request.run_id)
        if run is None:
            raise AgentStorageConflict("agent_continuation_run_missing")
        call_item = next(
            (
                item
                for item in await self._runs.list_items(run.run_id)
                if item.item_id == request.call_item_id
                and item.kind is AgentItemKind.TOOL_CALL
            ),
            None,
        )
        if call_item is None:
            raise AgentStorageConflict("agent_continuation_call_missing")
        owner_scope = str(
            request.request_metadata.get("agent_owner_scope") or ""
        ).strip()
        if not owner_scope:
            raise AgentStorageConflict("agent_continuation_owner_missing")
        locator = AgentContinuationLocatorService().build(
            run=run,
            call_item=call_item,
            owner_scope=owner_scope,
            resume_kind=resume_kind,
            authority_digest=authority_digest,
            pinned_bundle_revision=(
                str(
                    request.request_metadata.get("skill_bundle_revision") or ""
                ).strip()
                or None
            ),
        )
        safe_locator = locator.to_safe_dict()
        self._continuation_locators[call_item.item_id] = safe_locator
        return safe_locator

    async def commit_waiting_for_dependency(
        self,
        request: InvocationRequest,
        node: TaskNode,
        result: CapabilityExecutionResult,
        *,
        now: datetime,
        activity_payload: dict[str, Any],
    ) -> TaskNode:
        await self._persist_events(result)
        waiting = await self._storage.save_task_node(
            replace(node, status=NodeStatus.WAITING_FOR_DEPENDENCY)
        )
        safe_remote_task_ref = str(
            result.output_payload.get("safe_remote_task_ref") or ""
        ).strip()
        if not safe_remote_task_ref:
            raise AgentStorageConflict("agent_remote_task_reference_missing")
        conversation = await self._storage.get_conversation(request.conversation_id)
        if conversation is None:
            raise AgentStorageConflict("agent_remote_task_conversation_missing")
        authority_digest = hashlib.sha256(
            json.dumps(
                {
                    "node_id": request.node_id,
                    "safe_remote_task_ref": safe_remote_task_ref,
                    "task_id": request.task_id,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        continuation_locator = await self._build_continuation_locator(
            request,
            resume_kind=AgentResumeKind.MCP_REMOTE_TASK,
            authority_digest=authority_digest,
        )
        published = await self._storage.publish_mcp_remote_task_binding(
            conversation.username,
            request.task_id,
            safe_remote_task_ref,
            published_at=now,
            continuation_plan=continuation_locator,
        )
        if published is None:
            raise AgentStorageConflict("agent_remote_task_publication_failed")
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
        await self._lifecycle_event(
            request,
            "task.late_result_discarded",
            {"capability_id": request.capability_id, "partial_output_discarded": True},
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
        if failed is None:
            latest = await self._storage.get_task_node(node.node_id)
            if latest is None:
                raise AgentStorageConflict("agent_route_rejection_node_missing")
            return latest
        await self._lifecycle_event(
            request,
            "node.failed",
            {"code": rejection_code},
        )
        return failed

    async def _persist_events(self, result: CapabilityExecutionResult) -> None:
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


async def persist_agent_slot_interrupt_authority(
    storage: SlotStoragePort,
    interrupt: Interrupt,
    *,
    now: datetime,
) -> Interrupt:
    carrier = slot_collection_from_required_fields(interrupt.required_fields)
    if (
        carrier is None
        or int(carrier.get("schema_version") or 0)
        != SLOT_COLLECTION_V2_SCHEMA_VERSION
    ):
        return interrupt
    collection = slot_collection_model_from_carrier(carrier, now=now)
    if not collection.collection_id:
        return interrupt
    existing = await storage.get_slot_collection(collection.collection_id)
    if existing is None:
        await storage.save_slot_collection(collection)
        for event in slot_collection_bootstrap_events(collection, now=now):
            await storage.append_slot_event(event)
    else:
        prompt_key = f"slot:{existing.collection_id}:prompt:{collection.round}"
        if (
            await storage.get_slot_event_by_idempotency_key(
                existing.collection_id, prompt_key
            )
            is None
        ):
            merged = replace(
                existing,
                status=collection.status or existing.status,
                round=max(existing.round, collection.round),
                selected_schema_id=(
                    collection.selected_schema_id or existing.selected_schema_id
                ),
                selected_entrypoint=(
                    collection.selected_entrypoint or existing.selected_entrypoint
                ),
                schema_digest=collection.schema_digest or existing.schema_digest,
                schema_snapshot=dict(
                    collection.schema_snapshot or existing.schema_snapshot
                ),
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
            collection = (
                await storage.apply_slot_transition(
                    existing.collection_id,
                    existing.revision,
                    merged,
                    prompt_event,
                    idempotency_key=prompt_key,
                )
                or await storage.get_slot_collection(existing.collection_id)
                or existing
            )
        else:
            collection = existing
    required_fields = slot_collection_required_fields_ref(collection)
    locator = interrupt.required_fields.get("_agent_continuation")
    if isinstance(locator, Mapping):
        required_fields["_agent_continuation"] = dict(locator)
    return replace(interrupt, required_fields=required_fields)


def _resume_kind(interrupt: Interrupt) -> AgentResumeKind:
    if interrupt.reason_code == "mcp_tool_approval_required":
        return AgentResumeKind.MCP_APPROVAL
    if interrupt.reason_code in {"mcp_input_required", "mcp_remote_task_input_required"}:
        return AgentResumeKind.MCP_ELICITATION
    return AgentResumeKind.SKILL_INPUT
