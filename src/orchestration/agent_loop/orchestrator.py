from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable, Mapping

from src.core.enums import TaskStatus
from src.core.models import EventRecord, Task
from src.orchestration.models import UserMCPServerProfile

from .capability_invoker import AgentInvocationContextStore
from .final_output import AgentFinalOutputPublisher
from .models import (
    AgentCancellationToken,
    AgentModelBinding,
    AgentRun,
    AgentRunStatus,
    AgentStorageConflict,
    AgentUserMessageCommit,
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

    async def start_or_resume(
        self,
        request: AgentExecutionRequest,
        *,
        cancellation: AgentCancellationToken | None = None,
    ) -> AgentOrchestrationResult:
        task = await self._load_task(request.task_id)
        if task is None or task.conversation_id != request.conversation_id:
            raise AgentStorageConflict("agent_task_identity_mismatch")
        task = await self._ensure_task_running(task)
        run = await self._runs.get_run_for_task(request.task_id)
        created = run is None
        if run is None:
            run = await self._runs.create_run(
                AgentRun(
                    run_id=f"agent-run:{request.task_id}",
                    task_id=request.task_id,
                    conversation_id=request.conversation_id,
                    status=AgentRunStatus.RUNNING,
                    binding=self._binding_factory(request),
                )
            )
        elif run.binding != self._binding_factory(request):
            raise AgentStorageConflict("agent_run_binding_mismatch")
        initialized = await self._writer.commit_agent_user_message(
            AgentUserMessageCommit(
                run_id=run.run_id,
                expected_revision=run.revision,
                expected_claim_token=run.claim_token,
                text=request.user_message,
            )
        )
        run = initialized.run
        self._contexts.register(
            run.run_id,
            metadata={
                **dict(request.metadata),
                "agent_owner_scope": request.owner_scope,
            },
            current_user_input=request.user_message,
        )
        if created:
            await self._record_event(
                self._make_event(
                    task_id=request.task_id,
                    conversation_id=request.conversation_id,
                    event_type="task.graph_created",
                    payload={
                        "edge_count": 0,
                        "node_count": 0,
                        "root_node_id": None,
                    },
                )
            )
            await self._record_event(
                self._make_event(
                    task_id=request.task_id,
                    conversation_id=request.conversation_id,
                    event_type="agent.run.started",
                    payload={
                        "model_option_digests": dict(run.binding.option_digests),
                        "routing_mode": str(task.routing_mode),
                    },
                )
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
