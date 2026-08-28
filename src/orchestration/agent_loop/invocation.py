from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping, Protocol, TypeVar

from src.core.contracts import CapabilityExecutionRequest, CapabilityExecutionResult, ExecutorPort
from src.core.enums import TaskStatus
from src.core.models import Task, TaskNode
from src.orchestration.mcp_route_handoff import normalize_selected_mcp_route
from src.orchestration.instance_selector import InstanceSelector

from .models import AgentCancellationToken, AgentModelBinding


WaveItem = TypeVar("WaveItem")
WaveResult = TypeVar("WaveResult")
OwnershipBoundary = Callable[
    ["InvocationRequest", Callable[["InvocationRequest"], Awaitable[Any]]],
    Awaitable[Any],
]


_SYSTEM_NODE_METADATA_KEYS = frozenset(
    {
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


@dataclass(frozen=True, slots=True)
class InvocationRequest:
    capability_id: str
    conversation_id: str
    task_id: str
    node_id: str
    run_id: str | None = None
    call_item_id: str | None = None
    expected_revision: int | None = None
    expected_claim_token: str | None = None
    model_binding: AgentModelBinding | None = None
    cancellation: AgentCancellationToken | None = None
    input_payload: Mapping[str, Any] = field(default_factory=dict)
    dependency_outputs: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    request_metadata: Mapping[str, Any] = field(default_factory=dict)
    node_metadata: Mapping[str, Any] = field(default_factory=dict)
    available_server_ids: frozenset[str] = frozenset()
    pinned_server_id_present: bool = False
    pinned_server_id: Any = None


@dataclass(frozen=True, slots=True)
class InvocationResult:
    node: TaskNode
    output_payload: dict[str, Any]
    execution_result: CapabilityExecutionResult | None = field(default=None, repr=False)


class InvocationCommitPort(Protocol):
    async def assert_execution_owned(self, request: InvocationRequest) -> None: ...

    async def start_node(
        self,
        request: InvocationRequest,
        node: TaskNode,
        *,
        instance_id: str,
        started_at: datetime,
        activity_payload: dict[str, Any],
    ) -> TaskNode: ...

    async def get_task_snapshot(self, task_id: str) -> Task | None: ...

    async def get_node_snapshot(self, node_id: str) -> TaskNode | None: ...

    async def commit_completed(
        self,
        request: InvocationRequest,
        node: TaskNode,
        result: CapabilityExecutionResult,
        *,
        now: datetime,
        activity_payload: dict[str, Any],
    ) -> TaskNode: ...

    async def commit_failed(
        self,
        request: InvocationRequest,
        node: TaskNode,
        result: CapabilityExecutionResult,
        *,
        now: datetime,
        activity_payload: dict[str, Any],
    ) -> TaskNode: ...

    async def commit_waiting_for_input(
        self,
        request: InvocationRequest,
        node: TaskNode,
        result: CapabilityExecutionResult,
        *,
        now: datetime,
        activity_payload: dict[str, Any],
    ) -> TaskNode: ...

    async def commit_waiting_for_dependency(
        self,
        request: InvocationRequest,
        node: TaskNode,
        result: CapabilityExecutionResult,
        *,
        now: datetime,
        activity_payload: dict[str, Any],
    ) -> TaskNode: ...

    async def discard_late_result(
        self,
        request: InvocationRequest,
        node: TaskNode,
        result: CapabilityExecutionResult,
        *,
        activity_payload: dict[str, Any],
    ) -> TaskNode: ...

    async def commit_route_rejected(
        self,
        request: InvocationRequest,
        node: TaskNode,
        *,
        rejection_code: str,
        now: datetime,
        activity_payload: dict[str, Any],
    ) -> TaskNode: ...


class CapabilityInvocationService:
    """The single route-to-executor-to-durable-outcome lifecycle."""

    def __init__(
        self,
        *,
        instance_selector: InstanceSelector,
        executor: ExecutorPort,
        commit_port: InvocationCommitPort,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._instance_selector = instance_selector
        self._executor = executor
        self._commit_port = commit_port
        self._now = now_fn or self._utcnow_naive

    async def invoke(
        self,
        request: InvocationRequest,
        task_node: TaskNode,
        *,
        ownership_boundary: OwnershipBoundary | None = None,
    ) -> InvocationResult:
        async def begin(owned_request: InvocationRequest):
            await self._commit_port.assert_execution_owned(owned_request)
            route_handoff = normalize_selected_mcp_route(
                capability_id=owned_request.capability_id,
                input_payload=owned_request.input_payload,
                node_metadata=owned_request.node_metadata,
                pinned_server_id_present=owned_request.pinned_server_id_present,
                pinned_server_id=owned_request.pinned_server_id,
                available_server_ids=owned_request.available_server_ids,
            )
            normalized_request = replace(
                owned_request,
                node_metadata=dict(route_handoff.normalized_node_metadata),
            )
            activity_payload = node_activity_payload(
                normalized_request.capability_id,
                normalized_request.node_metadata,
            )
            if route_handoff.rejection_code is not None:
                rejected = await self._commit_port.commit_route_rejected(
                    normalized_request,
                    task_node,
                    rejection_code=route_handoff.rejection_code,
                    now=self._now(),
                    activity_payload=activity_payload,
                )
                return normalized_request, activity_payload, None, InvocationResult(
                    rejected, {}
                )

            instance = self._instance_selector.select_instance(
                normalized_request.capability_id
            )
            running = await self._commit_port.start_node(
                normalized_request,
                task_node,
                instance_id=instance.instance_id,
                started_at=task_node.started_at or self._now(),
                activity_payload={
                    **activity_payload,
                    "instance_id": instance.instance_id,
                },
            )
            return normalized_request, activity_payload, running, None

        normalized_request, activity_payload, running, early_result = (
            await self._run_ownership_boundary(
                request,
                begin,
                ownership_boundary,
            )
        )
        if early_result is not None:
            return early_result
        assert running is not None
        task_snapshot = await self._commit_port.get_task_snapshot(normalized_request.task_id)
        effective_metadata = task_authoritative_metadata(
            execution_metadata(
                normalized_request.request_metadata,
                normalized_request.node_metadata,
            ),
            task_snapshot,
        )
        result = await self._executor.execute(
            CapabilityExecutionRequest(
                capability_id=normalized_request.capability_id,
                conversation_id=normalized_request.conversation_id,
                task_id=normalized_request.task_id,
                node_id=normalized_request.node_id,
                input_payload=dict(normalized_request.input_payload),
                dependency_outputs={
                    dependency: dict(output)
                    for dependency, output in normalized_request.dependency_outputs.items()
                },
                metadata=effective_metadata,
            )
        )

        async def finish(owned_request: InvocationRequest) -> InvocationResult:
            await self._commit_port.assert_execution_owned(owned_request)
            latest_task = await self._commit_port.get_task_snapshot(
                owned_request.task_id
            )
            latest_node = (
                await self._commit_port.get_node_snapshot(owned_request.node_id)
                or running
            )
            if latest_task is not None and (
                latest_task.status != TaskStatus.RUNNING
                or latest_task.cancel_requested_at is not None
            ):
                discarded = await self._commit_port.discard_late_result(
                    owned_request,
                    latest_node,
                    result,
                    activity_payload=activity_payload,
                )
                return InvocationResult(discarded, {}, result)

            now = self._now()
            if result.interrupt is not None or (
                result.error is not None
                and result.error.code == "skill_input_missing"
            ):
                waiting = await self._commit_port.commit_waiting_for_input(
                    owned_request,
                    latest_node,
                    result,
                    now=now,
                    activity_payload=activity_payload,
                )
                return InvocationResult(
                    waiting,
                    {key: value for key, value in result.output_payload.items()},
                    result,
                )
            if result.error is not None:
                failed = await self._commit_port.commit_failed(
                    owned_request,
                    latest_node,
                    result,
                    now=now,
                    activity_payload=activity_payload,
                )
                return InvocationResult(
                    failed,
                    {key: value for key, value in result.output_payload.items()},
                    result,
                )
            if (
                owned_request.capability_id == "mcp.dispatch"
                and result.output_payload.get("mcp_status")
                == "remote_task_created"
            ):
                waiting = await self._commit_port.commit_waiting_for_dependency(
                    owned_request,
                    latest_node,
                    result,
                    now=now,
                    activity_payload=activity_payload,
                )
                return InvocationResult(
                    waiting,
                    {key: value for key, value in result.output_payload.items()},
                    result,
                )
            completed = await self._commit_port.commit_completed(
                owned_request,
                latest_node,
                result,
                now=now,
                activity_payload=activity_payload,
            )
            return InvocationResult(
                completed,
                {key: value for key, value in result.output_payload.items()},
                result,
            )

        return await self._run_ownership_boundary(
            normalized_request,
            finish,
            ownership_boundary,
        )

    @staticmethod
    async def _run_ownership_boundary(
        request: InvocationRequest,
        operation: Callable[[InvocationRequest], Awaitable[Any]],
        boundary: OwnershipBoundary | None,
    ) -> Any:
        if boundary is None:
            return await operation(request)
        return await boundary(request, operation)

    @staticmethod
    def _utcnow_naive() -> datetime:
        return datetime.now(timezone.utc).replace(tzinfo=None)


def node_activity_payload(
    capability_id: str,
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    payload: dict[str, Any] = {"capability_id": capability_id}
    for key in ("skill_name",):
        skill_name = metadata.get(key)
        if isinstance(skill_name, str) and skill_name.strip():
            payload["skill_name"] = skill_name.strip()
            break
    return payload


def execution_metadata(
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


def task_authoritative_metadata(
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


def deterministic_invocation_waves(
    items: tuple[WaveItem, ...],
    *,
    is_parallel_safe: Callable[[WaveItem], bool],
) -> tuple[tuple[WaveItem, ...], ...]:
    waves: list[tuple[WaveItem, ...]] = []
    pending_parallel: list[WaveItem] = []
    for item in items:
        if is_parallel_safe(item):
            pending_parallel.append(item)
            continue
        if pending_parallel:
            waves.append(tuple(pending_parallel))
            pending_parallel = []
        waves.append((item,))
    if pending_parallel:
        waves.append(tuple(pending_parallel))
    return tuple(waves)


async def execute_deterministic_waves(
    items: tuple[WaveItem, ...],
    *,
    is_parallel_safe: Callable[[WaveItem], bool],
    execute: Callable[[WaveItem], Awaitable[WaveResult]],
) -> tuple[tuple[tuple[WaveItem, WaveResult], ...], ...]:
    import asyncio

    completed: list[tuple[tuple[WaveItem, WaveResult], ...]] = []
    for wave in deterministic_invocation_waves(items, is_parallel_safe=is_parallel_safe):
        results = await asyncio.gather(*(execute(item) for item in wave))
        completed.append(tuple(zip(wave, results, strict=True)))
    return tuple(completed)
