from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Mapping

from src.core.enums import TaskStatus
from src.core.models import EventRecord, Task
from src.orchestration.models import UserMCPServerProfile

from .capability_invoker import AgentInvocationContextStore
from .final_output import AgentFinalOutputPublisher
from .models import (
    AgentCancellationToken,
    AgentItemKind,
    AgentItemState,
    AgentModelBinding,
    AgentRun,
    AgentRunStatus,
    AgentStorageConflict,
    AgentUserMessageCommit,
    AgentUserMessageCommitResult,
    provider_safe_tool_name,
)
from .repository import AgentAtomicWriter, AgentRunRepository
from .runner import AgentLoopRunResult, AgentLoopRunner
from .lease import AgentLeaseHandle
from .tool_catalog import CapabilityVisibilityContext


@dataclass(frozen=True, slots=True)
class AgentExecutionRequest:
    task_id: str
    conversation_id: str
    root_message_id: str
    user_message: str
    owner_scope: str
    requested_capability_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    current_user_message: str | None = None
    resolved_user_message: str | None = None
    memory_context: Mapping[str, Any] | None = None
    available_mcp_servers: tuple[UserMCPServerProfile, ...] = ()

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (
                self.task_id,
                self.conversation_id,
                self.root_message_id,
                self.user_message,
                self.owner_scope,
            )
        ):
            raise ValueError("agent_execution_request_identity_invalid")

    @property
    def effective_user_message(self) -> str:
        return self.resolved_user_message or self.current_user_message or self.user_message


@dataclass(frozen=True, slots=True)
class AgentOrchestrationResult:
    run: AgentRun
    state: str
    final_message_id: str | None = None


@dataclass(frozen=True, slots=True)
class _InitializedAgentRun:
    request: AgentExecutionRequest
    task: Task
    run: AgentRun


class AgentLoopOrchestrator:
    """The single production entry for starting or resuming an AgentRun."""

    def __init__(
        self,
        *,
        runs: AgentRunRepository,
        writer: AgentAtomicWriter,
        runner: AgentLoopRunner,
        final_output: AgentFinalOutputPublisher,
        contexts: AgentInvocationContextStore,
        task_loader: Callable[[str], Awaitable[Task | None]],
        task_cas: Callable[..., Awaitable[Task | None]],
        binding_factory: Callable[[AgentExecutionRequest], AgentModelBinding],
        record_event: Callable[[EventRecord], Awaitable[None]],
        make_event: Callable[..., EventRecord],
        event_loader: Callable[[str, str], Awaitable[EventRecord | None]] | None = None,
        initialization_event_recorder: (
            Callable[[EventRecord], Awaitable[bool]] | None
        ) = None,
    ) -> None:
        self._runs = runs
        self._writer = writer
        self._runner = runner
        self._final_output = final_output
        self._contexts = contexts
        self._load_task = task_loader
        self._task_cas = task_cas
        self._binding_factory = binding_factory
        self._record_event = record_event
        self._make_event = make_event
        self._load_event = event_loader
        self._record_initialization_event = initialization_event_recorder

    async def start_or_resume(
        self,
        request: AgentExecutionRequest,
        *,
        cancellation: AgentCancellationToken | None = None,
    ) -> AgentOrchestrationResult:
        initialized = await self.initialize_run(request)
        return await self.run_initialized(initialized, cancellation=cancellation)

    async def initialize_run(
        self,
        request: AgentExecutionRequest,
    ) -> _InitializedAgentRun:
        task = await self._load_task(request.task_id)
        if not _task_matches_request(task, request):
            raise AgentStorageConflict("agent_task_identity_mismatch")
        assert task is not None
        task = await self._ensure_task_running(task)
        if not _task_matches_request(task, request):
            raise AgentStorageConflict("agent_task_identity_mismatch")
        binding = self._binding_factory(request)
        expected_run = AgentRun(
            run_id=_agent_run_id(request.task_id),
            task_id=request.task_id,
            conversation_id=request.conversation_id,
            status=AgentRunStatus.RUNNING,
            binding=binding,
        )
        run = await self._runs.get_run_for_task(request.task_id)
        if run is None:
            try:
                run = await self._runs.create_run(expected_run)
            except AgentStorageConflict as create_error:
                if str(create_error) != "agent_run_task_already_bound":
                    raise
                run = await self._runs.get_run_for_task(request.task_id)
                if run is None:
                    raise create_error
        _validate_run_identity(run, expected_run, check_binding=True)
        initialized = await self._writer.commit_agent_user_message(
            AgentUserMessageCommit(
                run_id=run.run_id,
                expected_revision=run.revision,
                expected_claim_token=run.claim_token,
                text=request.user_message,
            )
        )
        run = initialized.run
        _validate_initialized_user_item(initialized, expected_run)
        if initialized.item.committed_at is None:
            raise AgentStorageConflict("agent_user_message_committed_at_missing")
        started_event_at = initialized.item.committed_at
        if run.created_at is not None and started_event_at <= run.created_at:
            started_event_at = run.created_at + timedelta(microseconds=1)
        await self._ensure_initialization_event(
            self._make_event(
                task_id=request.task_id,
                conversation_id=request.conversation_id,
                event_type="task.graph_created",
                payload={
                    "edge_count": 0,
                    "node_count": 0,
                },
            ),
            event_id=f"evt-agent-task-graph-created:{request.task_id}",
            created_at=run.created_at,
        )
        await self._ensure_initialization_event(
            self._make_event(
                task_id=request.task_id,
                conversation_id=request.conversation_id,
                event_type="agent.run.started",
                payload={
                    "model_option_digests": dict(run.binding.option_digests),
                    "routing_mode": str(task.routing_mode),
                },
            ),
            event_id=f"evt-agent-run-started:{run.run_id}",
            created_at=started_event_at,
        )
        return _InitializedAgentRun(request=request, task=task, run=run)

    async def run_initialized(
        self,
        initialized: _InitializedAgentRun,
        *,
        cancellation: AgentCancellationToken | None = None,
    ) -> AgentOrchestrationResult:
        request = initialized.request
        task = initialized.task
        run = initialized.run
        if not _task_matches_request(task, request):
            raise AgentStorageConflict("agent_task_identity_mismatch")
        _validate_run_identity(
            run,
            AgentRun(
                run_id=_agent_run_id(request.task_id),
                task_id=request.task_id,
                conversation_id=request.conversation_id,
                status=run.status,
                binding=run.binding,
            ),
            check_binding=False,
        )
        self._contexts.register(
            run.run_id,
            metadata={
                **dict(request.metadata),
                "agent_owner_scope": request.owner_scope,
            },
            current_user_input=request.user_message,
        )
        visibility = CapabilityVisibilityContext(
            authenticated_owner_scope=request.owner_scope,
            execution_path=str(task.mcp_execution_mode or "default"),
            pinned_skill_bundle_revision=str(
                request.metadata.get("skill_bundle_revision") or ""
            ).strip()
            or None,
            safe_mcp_server_profiles=request.available_mcp_servers,
        )
        required_name = (
            provider_safe_tool_name(request.requested_capability_id)
            if request.requested_capability_id
            else None
        )
        loop_result = await self._runner.run(
            run.run_id,
            initial_required_tool_name=required_name,
            trusted_facts=_trusted_facts(
                request.metadata,
                memory_context=request.memory_context,
            ),
            visibility_context=visibility,
            cancellation=cancellation,
        )
        return await self._finish(loop_result)

    async def _ensure_initialization_event(
        self,
        event: EventRecord,
        *,
        event_id: str,
        created_at: datetime | None,
    ) -> None:
        if created_at is None:
            raise AgentStorageConflict("agent_run_created_at_missing")
        expected = replace(event, event_id=event_id, created_at=created_at)
        if self._record_initialization_event is not None:
            await self._record_initialization_event(expected)
            return
        if self._load_event is not None:
            existing = await self._load_event(expected.task_id, event_id)
            if existing is not None:
                if existing != expected:
                    raise AgentStorageConflict("agent_initialization_event_conflict")
                return
        await self._record_event(expected)
        if self._load_event is not None:
            stored = await self._load_event(expected.task_id, event_id)
            if stored != expected:
                raise AgentStorageConflict("agent_initialization_event_write_conflict")

    async def _finish(
        self, result: AgentLoopRunResult
    ) -> AgentOrchestrationResult:
        if result.state != "final_candidate":
            return AgentOrchestrationResult(result.run, result.state)
        if result.final_candidate is None:
            raise AgentStorageConflict("agent_final_candidate_missing")
        final = await self._final_output.publish(
            run_id=result.run.run_id,
            candidate_item_id=result.final_candidate.item_id,
            handle=result.lease_handle,
        )
        self._contexts.release(final.run.run_id)
        await self._record_event(
            self._make_event(
                task_id=final.run.task_id,
                conversation_id=final.run.conversation_id,
                event_type="agent.run.completed",
                payload={
                    "compaction_count": 0,
                    "duration_seconds": 0,
                    "outcome": "completed",
                    "sample_count": 0,
                    "tool_call_count": final.run.next_batch_call_ordinal,
                },
            )
        )
        return AgentOrchestrationResult(
            final.run,
            "completed",
            final_message_id=final.message_id,
        )

    async def run_claimed(
        self,
        run_id: str,
        *,
        handle: AgentLeaseHandle,
        initial_required_tool_name: str | None = None,
        trusted_facts: tuple[str, ...] = (),
        current_user_input: str | None = None,
        visibility_context: CapabilityVisibilityContext | None = None,
        cancellation: AgentCancellationToken | None = None,
    ) -> AgentOrchestrationResult:
        result = await self._runner.run_claimed(
            run_id,
            handle=handle,
            initial_required_tool_name=initial_required_tool_name,
            trusted_facts=trusted_facts,
            current_user_input=current_user_input,
            visibility_context=visibility_context,
            cancellation=cancellation,
        )
        return await self._finish(result)

    async def cancel(self, task_id: str, *, reason_code: str) -> AgentRun | None:
        run = await self._runs.get_run_for_task(task_id)
        if run is None or run.status in {
            AgentRunStatus.COMPLETED,
            AgentRunStatus.FAILED,
            AgentRunStatus.CANCELLED,
        }:
            return run
        cancelled = await self._writer.cancel_agent_run(
            run.run_id,
            expected_revision=run.revision,
            expected_claim_token=run.claim_token,
            safe_reason_code=reason_code,
        )
        self._contexts.release(run.run_id)
        return cancelled

    async def fail(self, task_id: str, *, error_code: str) -> AgentRun | None:
        run = await self._runs.get_run_for_task(task_id)
        if run is None or run.status in {
            AgentRunStatus.COMPLETED,
            AgentRunStatus.FAILED,
            AgentRunStatus.CANCELLED,
        }:
            return run
        failed = await self._writer.fail_agent_run(
            run.run_id,
            expected_revision=run.revision,
            expected_claim_token=run.claim_token,
            safe_error_code=error_code,
        )
        self._contexts.release(run.run_id)
        return failed

    async def _ensure_task_running(self, task: Task) -> Task:
        if task.status == TaskStatus.RUNNING:
            return task
        if task.status not in {TaskStatus.ACCEPTED, TaskStatus.PLANNING}:
            raise AgentStorageConflict("agent_task_not_startable")
        running = await self._task_cas(
            replace(task, status=TaskStatus.RUNNING),
            expected_from_status=task.status,
        )
        if running is None:
            latest = await self._load_task(task.task_id)
            if latest is None or latest.status != TaskStatus.RUNNING:
                raise AgentStorageConflict("agent_task_start_conflict")
            return latest
        return running


def _agent_run_id(task_id: str) -> str:
    return f"agent-run:{task_id}"


def _task_matches_request(
    task: Task | None,
    request: AgentExecutionRequest,
) -> bool:
    return bool(
        task is not None
        and task.task_id == request.task_id
        and task.conversation_id == request.conversation_id
        and task.root_message_id == request.root_message_id
    )


def _validate_run_identity(
    run: AgentRun,
    expected: AgentRun,
    *,
    check_binding: bool,
) -> None:
    if (
        run.run_id != expected.run_id
        or run.task_id != expected.task_id
        or run.conversation_id != expected.conversation_id
    ):
        raise AgentStorageConflict("agent_run_identity_mismatch")
    if check_binding and run.binding != expected.binding:
        raise AgentStorageConflict("agent_run_binding_mismatch")


def _validate_initialized_user_item(
    initialized: AgentUserMessageCommitResult,
    expected_run: AgentRun,
) -> None:
    run = initialized.run
    item = initialized.item
    _validate_run_identity(run, expected_run, check_binding=True)
    if (
        item.item_id != f"agent-item:{expected_run.run_id}:user-initial"
        or item.run_id != expected_run.run_id
        or item.task_id != expected_run.task_id
        or item.sequence != 1
        or item.kind is not AgentItemKind.USER_MESSAGE
        or item.state is not AgentItemState.COMMITTED
    ):
        raise AgentStorageConflict("agent_user_message_identity_mismatch")


def model_binding_digest(*, name: str, value: str) -> str:
    return hashlib.sha256(f"{name}\0{value}".encode()).hexdigest()


_TRUSTED_FACT_KEYS = frozenset(
    {
        "capability_missing_fallback",
        "conversation_memory",
        "mcp_remote_task_result_projection",
        "slot_collection",
        "uploaded_artifacts",
    }
)


def _trusted_facts(
    metadata: Mapping[str, Any],
    *,
    memory_context: Mapping[str, Any] | None = None,
) -> tuple[str, ...]:
    selected = {
        key: metadata[key]
        for key in sorted(_TRUSTED_FACT_KEYS)
        if key in metadata
    }
    if memory_context is not None:
        selected["conversation_memory"] = dict(memory_context)
    if not selected:
        return ()
    import json

    return (
        json.dumps(
            selected,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ),
    )
