from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, Protocol

from src.core.enums import EventVisibility, MessageRole, TaskStatus
from src.core.models import EventRecord, Message, Task, TaskNode
from src.orchestration.agent_loop.models import (
    AgentItemKind,
    AgentItemState,
    AgentRun,
    AgentRunStatus,
)
from src.orchestration.agent_loop.repository import AgentRunRepository

from .dto import TaskGraphResponse, TaskNodeResponse


class AgentTaskProjectionStore(Protocol):
    async def get_task(self, task_id: str) -> Task | None: ...

    async def list_task_nodes_for_task(self, task_id: str) -> list[TaskNode]: ...

    async def list_tasks_for_conversation(
        self,
        conversation_id: str,
        statuses: set[TaskStatus] | None = None,
    ) -> list[Task]: ...


_TASK_STATUS_BY_RUN = {
    AgentRunStatus.RUNNING: TaskStatus.RUNNING,
    AgentRunStatus.WAITING_FOR_INPUT: TaskStatus.RUNNING,
    AgentRunStatus.WAITING_FOR_DEPENDENCY: TaskStatus.RUNNING,
    AgentRunStatus.COMPLETED: TaskStatus.COMPLETED,
    AgentRunStatus.FAILED: TaskStatus.FAILED,
    AgentRunStatus.CANCELLED: TaskStatus.CANCELLED,
}


class AgentTaskProjectionService:
    """Read-only Agent-to-public Task projection; no TaskEdge dependency is accepted."""

    def __init__(
        self,
        *,
        runs: AgentRunRepository,
        tasks: AgentTaskProjectionStore,
    ) -> None:
        self._runs = runs
        self._tasks = tasks

    async def get_agent_run(self, task_id: str) -> AgentRun | None:
        run = await self._runs.get_run_for_task(task_id)
        if run is None:
            return None
        task = await self._tasks.get_task(task_id)
        if task is None or task.conversation_id != run.conversation_id:
            raise ValueError("agent_task_projection_identity_mismatch")
        return run

    async def project_graph(self, task_id: str) -> TaskGraphResponse | None:
        if await self.get_agent_run(task_id) is None:
            return None
        nodes = await self._tasks.list_task_nodes_for_task(task_id)
        return TaskGraphResponse(
            task_id=task_id,
            nodes=[
                TaskNodeResponse(
                    node_id=node.node_id,
                    capability_id=node.capability_id,
                    status=str(node.status),
                    criticality="required",
                    dependency_type="hard",
                    assigned_instance_id=node.assigned_instance_id,
                    started_at=node.started_at,
                    finished_at=node.finished_at,
                )
                for node in nodes
            ],
            edges=[],
        )

    async def project_history_messages(
        self,
        conversation_id: str,
        messages: list[Message],
    ) -> list[Message]:
        projected = [
            replace(message, stream_status="complete")
            if str(message.role) == str(MessageRole.ASSISTANT)
            and message.stream_status == "completed"
            else message
            for message in messages
        ]
        existing_task_ids = {
            message.task_id
            for message in projected
            if message.task_id is not None
            and str(message.role) == str(MessageRole.ASSISTANT)
            and message.stream_status in {"complete", "completed"}
        }
        tasks = await self._tasks.list_tasks_for_conversation(conversation_id)
        for task in tasks:
            if task.task_id in existing_task_ids:
                continue
            run = await self._runs.get_run_for_task(task.task_id)
            if run is None or run.status is not AgentRunStatus.COMPLETED:
                continue
            if await self.get_agent_run(task.task_id) is None:
                continue
            items = await self._runs.list_items(run.run_id)
            final_item = next(
                (
                    item
                    for item in items
                    if item.item_id == run.active_sample_item_id
                    and item.kind is AgentItemKind.ASSISTANT_MESSAGE
                    and item.state is AgentItemState.COMMITTED
                ),
                None,
            )
            if final_item is None:
                raise ValueError("agent_history_final_item_missing")
            try:
                payload = json.loads(final_item.payload_json)
            except json.JSONDecodeError as exc:
                raise ValueError("agent_history_final_payload_invalid") from exc
            text = payload.get("text") if isinstance(payload, dict) else None
            if not isinstance(text, str) or not text.strip():
                raise ValueError("agent_history_final_payload_invalid")
            projected.append(
                Message(
                    message_id=f"agent-message:{task.task_id}:final",
                    conversation_id=conversation_id,
                    role=MessageRole.ASSISTANT,
                    content=text,
                    task_id=task.task_id,
                    stream_status="complete",
                    created_at=final_item.committed_at,
                    message_type="chat",
                    metadata={"source": "agent_final_output"},
                    updated_at=final_item.committed_at,
                )
            )
        projected.sort(
            key=lambda message: (
                message.created_at is None,
                "" if message.created_at is None else message.created_at.isoformat(),
                message.message_id,
            )
        )
        return projected


@dataclass(frozen=True, slots=True)
class _AgentEventSpec:
    fields: frozenset[str]
    visibility: EventVisibility


_AGENT_EVENT_SPECS = {
    "agent.run.started": _AgentEventSpec(
        frozenset({"model_option_digests", "routing_mode"}),
        EventVisibility.AUDIT_ONLY,
    ),
    "agent.sample.started": _AgentEventSpec(
        frozenset({"sample_id"}),
        EventVisibility.AUDIT_ONLY,
    ),
    "agent.sample.completed": _AgentEventSpec(
        frozenset(
            {
                "duration_seconds",
                "outcome",
                "sample_id",
                "tool_count",
                "usage_status",
            }
        ),
        EventVisibility.AUDIT_ONLY,
    ),
    "agent.tool_call.accepted": _AgentEventSpec(
        frozenset(
            {"argument_digest", "call_id", "capability_kind", "ordinal"}
        ),
        EventVisibility.AUDIT_ONLY,
    ),
    "agent.tool_result.committed": _AgentEventSpec(
        frozenset(
            {"artifact_count", "call_id", "error_code", "result_digest", "status"}
        ),
        EventVisibility.AUDIT_ONLY,
    ),
    "agent.run.waiting": _AgentEventSpec(
        frozenset({"interrupt_id", "reason_kind", "remaining_count"}),
        EventVisibility.FRONTEND,
    ),
    "agent.run.resumed": _AgentEventSpec(
        frozenset({"outcome", "remaining_count"}),
        EventVisibility.FRONTEND,
    ),
    "agent.run.lease_lost": _AgentEventSpec(
        frozenset({"lease_revision", "phase", "reason_code"}),
        EventVisibility.AUDIT_ONLY,
    ),
    "agent.context.compacted": _AgentEventSpec(
        frozenset(
            {
                "completion_tokens",
                "covered_end_sequence",
                "covered_start_sequence",
                "duration_seconds",
                "outcome",
                "prompt_tokens",
                "source_digest",
            }
        ),
        EventVisibility.AUDIT_ONLY,
    ),
}
for _terminal_event in (
    "agent.run.completed",
    "agent.run.cancelled",
):
    _AGENT_EVENT_SPECS[_terminal_event] = _AgentEventSpec(
        frozenset(
            {
                "compaction_count",
                "duration_seconds",
                "outcome",
                "sample_count",
                "tool_call_count",
            }
        ),
        EventVisibility.FRONTEND,
    )
_AGENT_EVENT_SPECS["agent.run.failed"] = _AgentEventSpec(
    frozenset(
        {
            "code",
            "compaction_count",
            "duration_seconds",
            "outcome",
            "sample_count",
            "tool_call_count",
        }
    ),
    EventVisibility.FRONTEND,
)


class AgentEventProjector:
    _FORBIDDEN_KEYS = frozenset(
        {
            "arguments",
            "attachment_body",
            "content",
            "credential",
            "credentials",
            "prompt",
            "raw_result",
            "result",
            "text",
            "token",
            "user_id",
            "username",
        }
    )
    _OUTCOMES = frozenset(
        {
            "aborted",
            "acquired",
            "cancelled",
            "completed",
            "duplicate",
            "failed",
            "lease_conflict",
            "lease_lost",
            "rejected",
            "renewed",
            "resumed",
            "waiting",
        }
    )
    _CAPABILITY_KINDS = frozenset({"internal", "mcp", "skill", "unknown"})
    _REASON_KINDS = frozenset(
        {"mcp_approval", "mcp_elicitation", "mcp_remote_task", "skill_input"}
    )
    _PHASES = frozenset(
        {"capability_wave", "compaction", "final_publish", "model_sample", "recovery"}
    )

    def graph_created(
        self,
        *,
        event_id: str,
        conversation_id: str,
        task_id: str,
    ) -> EventRecord:
        return EventRecord(
            event_id=_bounded_id(event_id),
            conversation_id=_bounded_id(conversation_id),
            task_id=_bounded_id(task_id),
            event_type="task.graph_created",
            payload={"edge_count": 0, "node_count": 0},
            visibility=EventVisibility.FRONTEND,
        )

    def durable(
        self,
        *,
        event_id: str,
        conversation_id: str,
        task_id: str,
        event_type: str,
        payload: Mapping[str, Any],
        node_id: str | None = None,
    ) -> EventRecord:
        spec = _AGENT_EVENT_SPECS.get(event_type)
        if spec is None or set(payload) != spec.fields:
            raise ValueError("agent_event_contract_invalid")
        self._validate_payload(payload)
        return EventRecord(
            event_id=_bounded_id(event_id),
            conversation_id=_bounded_id(conversation_id),
            task_id=_bounded_id(task_id),
            node_id=None if node_id is None else _bounded_id(node_id),
            event_type=event_type,
            payload=dict(payload),
            visibility=spec.visibility,
        )

    def reasoning_delta(
        self,
        *,
        event_id: str,
        conversation_id: str,
        task_id: str,
        sample_id: str,
        ordinal: int,
        delta: str,
        node_id: str | None = None,
    ) -> EventRecord:
        if not isinstance(delta, str) or not delta or not _non_negative_int(ordinal):
            raise ValueError("agent_reasoning_delta_invalid")
        return EventRecord(
            event_id=_bounded_id(event_id),
            conversation_id=_bounded_id(conversation_id),
            task_id=_bounded_id(task_id),
            node_id=None if node_id is None else _bounded_id(node_id),
            event_type="agent.reasoning_delta",
            payload={
                "delta": delta,
                "ordinal": ordinal,
                "sample_id": _bounded_id(sample_id),
            },
            visibility=EventVisibility.FRONTEND,
        )

    def reasoning_reset(
        self,
        *,
        event_id: str,
        conversation_id: str,
        task_id: str,
        sample_id: str,
        node_id: str | None = None,
    ) -> EventRecord:
        return EventRecord(
            event_id=_bounded_id(event_id),
            conversation_id=_bounded_id(conversation_id),
            task_id=_bounded_id(task_id),
            node_id=None if node_id is None else _bounded_id(node_id),
            event_type="agent.reasoning_reset",
            payload={"sample_id": _bounded_id(sample_id)},
            visibility=EventVisibility.FRONTEND,
        )

    def _validate_payload(self, payload: Mapping[str, Any]) -> None:
        _reject_forbidden_keys(payload, self._FORBIDDEN_KEYS)
        for key, value in payload.items():
            if key.endswith("_digest"):
                _require_digest(value)
            elif key == "model_option_digests":
                if not isinstance(value, Mapping) or any(
                    not isinstance(name, str) or not name or not _is_digest(digest)
                    for name, digest in value.items()
                ):
                    raise ValueError("agent_event_digest_invalid")
            elif key in {
                "artifact_count",
                "compaction_count",
                "covered_end_sequence",
                "covered_start_sequence",
                "lease_revision",
                "ordinal",
                "remaining_count",
                "sample_count",
                "tool_call_count",
                "tool_count",
            }:
                if not _non_negative_int(value):
                    raise ValueError("agent_event_count_invalid")
            elif key in {"completion_tokens", "prompt_tokens"}:
                if value is not None and not _non_negative_int(value):
                    raise ValueError("agent_event_token_count_invalid")
            elif key == "duration_seconds":
                if (
                    isinstance(value, bool)
                    or not isinstance(value, int | float)
                    or not math.isfinite(value)
                    or value < 0
                ):
                    raise ValueError("agent_event_duration_invalid")
            elif key == "outcome" and value not in self._OUTCOMES:
                raise ValueError("agent_event_outcome_invalid")
            elif key == "status" and value not in {
                "aborted",
                "completed",
                "failed",
                "waiting_for_dependency",
                "waiting_for_input",
            }:
                raise ValueError("agent_event_status_invalid")
            elif key == "usage_status" and value not in {
                "available",
                "usage_unavailable",
            }:
                raise ValueError("agent_event_usage_status_invalid")
            elif key == "capability_kind" and value not in self._CAPABILITY_KINDS:
                raise ValueError("agent_event_capability_kind_invalid")
            elif key == "reason_kind" and value not in self._REASON_KINDS:
                raise ValueError("agent_event_reason_kind_invalid")
            elif key == "phase" and value not in self._PHASES:
                raise ValueError("agent_event_phase_invalid")
            elif key in {"call_id", "interrupt_id", "sample_id"}:
                _bounded_id(value)
            elif key == "routing_mode" and value not in {"auto", "force_capability"}:
                raise ValueError("agent_event_routing_mode_invalid")
            elif key in {"code", "error_code", "reason_code"}:
                if value is not None and (
                    not isinstance(value, str)
                    or re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,127}", value) is None
                ):
                    raise ValueError("agent_event_code_invalid")


def _bounded_id(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value.encode()) > 512:
        raise ValueError("agent_projection_id_invalid")
    return value


def _is_digest(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _require_digest(value: Any) -> None:
    if not _is_digest(value):
        raise ValueError("agent_event_digest_invalid")


def _non_negative_int(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value >= 0


def _reject_forbidden_keys(value: Any, forbidden: frozenset[str]) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if str(key).lower() in forbidden:
                raise ValueError("agent_event_payload_unsafe")
            _reject_forbidden_keys(nested, forbidden)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _reject_forbidden_keys(nested, forbidden)


__all__ = [
    "AgentEventProjector",
    "AgentTaskProjectionService",
]
