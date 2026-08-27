from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, replace
from typing import Any, Mapping, Protocol

from .context import AgentContextBuilder
from .invocation import deterministic_invocation_waves
from .lease import AgentLeaseController, AgentLeaseHandle
from .model_port import AgentModelPort
from .models import (
    AgentCallOutcomeCommit,
    AgentCallOutcomeStatus,
    AgentCancellationToken,
    AgentItem,
    AgentItemKind,
    AgentItemState,
    AgentRun,
    AgentRunStatus,
    AgentSampleCommit,
    AgentStagedArtifact,
    AgentStorageConflict,
    AgentToolCall,
    AgentToolChoice,
)
from .repository import AgentAtomicWriter, AgentRunRepository
from .tool_catalog import (
    AgentToolCatalogBuilder,
    CapabilityInvocationPolicy,
    CapabilityVisibilityContext,
)


MAX_AGENT_REASONING_BYTES = 524_288
AGENT_REASONING_TRUNCATED_MARKER = "思考内容过长，已截断"
_AGENT_REASONING_TRUNCATED_MARKER_BYTES = len(
    AGENT_REASONING_TRUNCATED_MARKER.encode("utf-8")
)


@dataclass(frozen=True, slots=True)
class AgentCallExecution:
    status: AgentCallOutcomeStatus
    safe_result_payload: Any = None
    staged_artifacts: tuple[AgentStagedArtifact, ...] = ()
    safe_error_code: str | None = None
    skill_activation_item: AgentItem | None = None


class AgentCallInvoker(Protocol):
    async def invoke(
        self,
        *,
        run: AgentRun,
        call: AgentToolCall,
        call_item: AgentItem,
        result_reservation: AgentItem,
        capability_id: str,
        effective_payload: Mapping[str, Any],
        cancellation: AgentCancellationToken | None,
        lease_handle: AgentLeaseHandle,
    ) -> AgentCallExecution: ...


@dataclass(frozen=True, slots=True)
class AgentLoopRunResult:
    run: AgentRun
    state: str
    lease_handle: AgentLeaseHandle
    final_candidate: AgentItem | None = None


class AgentLoopRunner:
    """Durable provider-neutral Agent loop state machine."""

    def __init__(
        self,
        *,
        runs: AgentRunRepository,
        writer: AgentAtomicWriter,
        model: AgentModelPort,
        context_builder: AgentContextBuilder,
        catalog_builder: AgentToolCatalogBuilder,
        visibility_context: CapabilityVisibilityContext,
        lease_controller: AgentLeaseController,
        invoker: AgentCallInvoker,
        owner_id: str,
        reasoning_delta_sink=None,
        reasoning_reset_sink=None,
    ) -> None:
        self._runs = runs
        self._writer = writer
        self._model = model
        self._context_builder = context_builder
        self._catalog_builder = catalog_builder
        self._visibility = visibility_context
        self._leases = lease_controller
        self._invoker = invoker
        self._owner_id = owner_id
        self._reasoning_delta_sink = reasoning_delta_sink
        self._reasoning_reset_sink = reasoning_reset_sink

    async def run(
        self,
        run_id: str,
        *,
        initial_required_tool_name: str | None = None,
        trusted_facts: tuple[str, ...] = (),
        current_user_input: str | None = None,
        visibility_context: CapabilityVisibilityContext | None = None,
        cancellation: AgentCancellationToken | None = None,
    ) -> AgentLoopRunResult:
        handle = await self._leases.acquire(run_id, owner_id=self._owner_id)
        return await self.run_claimed(
            run_id,
            handle=handle,
            initial_required_tool_name=initial_required_tool_name,
            trusted_facts=trusted_facts,
            current_user_input=current_user_input,
            visibility_context=visibility_context,
            cancellation=cancellation,
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
    ) -> AgentLoopRunResult:
        if handle.current.run_id != run_id:
            raise AgentStorageConflict("agent_task_lease_run_mismatch")
        required_tool_name = initial_required_tool_name
        reasoning_bytes_published = 0
        reasoning_truncated = False
        while True:
            self._check_cancel(cancellation)
            run = await self._require_active_run(run_id)
            items = await self._runs.list_items(run_id)
            if run.status in {
                AgentRunStatus.WAITING_FOR_INPUT,
                AgentRunStatus.WAITING_FOR_DEPENDENCY,
            }:
                released = await self._leases.release_waiting(run_id, handle=handle)
                return AgentLoopRunResult(
                    run=released,
                    state="waiting",
                    lease_handle=handle,
                )
            visibility = visibility_context or self._visibility
            catalog = self._catalog_builder.build(visibility)
            pending_records = _pending_active_batch(run, items)
            if pending_records:
                waiting = await self._execute_records(
                    run,
                    pending_records,
                    catalog.policies,
                    {
                        tool.provider_safe_name: tool.capability_id
                        for tool in catalog.tools
                    },
                    handle,
                    cancellation,
                    visibility,
                )
                if waiting is not None:
                    return waiting
                continue
            choice = (
                AgentToolChoice("required", required_tool_name)
                if required_tool_name is not None
                else AgentToolChoice()
            )
            model_request = self._context_builder.build(
                run=run,
                items=items,
                catalog=catalog,
                trusted_facts=trusted_facts,
                current_user_input=current_user_input,
                tool_choice=choice,
            )
            model_request = replace(model_request, cancellation=cancellation)
            if self._reasoning_delta_sink is not None:
                reasoning_ordinal = 0
                reasoning_reset_ordinal = 0
                sample_id = f"agent-sample:{run.run_id}:r{run.revision}"

                async def publish_reasoning(delta: str) -> None:
                    nonlocal reasoning_bytes_published, reasoning_ordinal, reasoning_truncated
                    if not delta or reasoning_truncated:
                        return
                    content_limit = (
                        MAX_AGENT_REASONING_BYTES
                        - _AGENT_REASONING_TRUNCATED_MARKER_BYTES
                    )
                    remaining = max(0, content_limit - reasoning_bytes_published)
                    encoded = delta.encode("utf-8")
                    if len(encoded) <= remaining:
                        reasoning_ordinal += 1
                        await self._reasoning_delta_sink(
                            run,
                            delta,
                            reasoning_ordinal,
                        )
                        reasoning_bytes_published += len(encoded)
                        return

                    fragment = _truncate_utf8(delta, remaining)
                    if fragment:
                        reasoning_ordinal += 1
                        await self._reasoning_delta_sink(
                            run,
                            fragment,
                            reasoning_ordinal,
                        )
                        reasoning_bytes_published += len(fragment.encode("utf-8"))
                    reasoning_truncated = True
                    reasoning_ordinal += 1
                    await self._reasoning_delta_sink(
                        run,
                        AGENT_REASONING_TRUNCATED_MARKER,
                        reasoning_ordinal,
                    )
                    reasoning_bytes_published += _AGENT_REASONING_TRUNCATED_MARKER_BYTES

                async def reset_reasoning() -> None:
                    nonlocal reasoning_reset_ordinal
                    if self._reasoning_reset_sink is None:
                        return
                    reasoning_reset_ordinal += 1
                    await self._reasoning_reset_sink(
                        run,
                        sample_id,
                        reasoning_reset_ordinal,
                    )

                model_request = replace(
                    model_request,
                    reasoning_delta_sink=publish_reasoning,
                    reasoning_reset_sink=reset_reasoning,
                )
            sample = await self._leases.run_active_phase(
                "model_sample",
                handle,
                lambda _handle: self._model.sample_agent(model_request),
            )
            self._check_cancel(cancellation)
            latest = await self._require_active_run(run_id)
            committed = await self._writer.commit_agent_sample(
                AgentSampleCommit(
                    run_id=run_id,
                    expected_revision=latest.revision,
                    expected_claim_token=handle.current.token,
                    sample=sample,
                    capability_ids_by_tool_name={
                        tool.provider_safe_name: tool.capability_id
                        for tool in catalog.tools
                    },
                )
            )
            if not sample.tool_calls:
                return AgentLoopRunResult(
                    run=committed.run,
                    state="final_candidate",
                    lease_handle=handle,
                    final_candidate=committed.assistant_item,
                )
            required_tool_name = None

    async def _execute_records(
        self,
        run: AgentRun,
        records: tuple[tuple[AgentToolCall, AgentItem, AgentItem], ...],
        policies: Mapping[str, CapabilityInvocationPolicy],
        tool_to_capability: Mapping[str, str],
        handle: AgentLeaseHandle,
        cancellation: AgentCancellationToken | None,
        visibility: CapabilityVisibilityContext,
    ) -> AgentLoopRunResult | None:
        for wave in deterministic_invocation_waves(
            records,
            is_parallel_safe=lambda record: _parallel_safe(
                record[0], tool_to_capability, policies
            ),
        ):
            self._check_cancel(cancellation)
            wave_results = await self._leases.run_active_phase(
                "capability_wave",
                handle,
                lambda _handle, current_wave=wave: self._execute_wave(
                    run,
                    current_wave,
                    tool_to_capability,
                    policies,
                    cancellation,
                    visibility,
                    _handle,
                ),
            )
            for (_, call_item, _), outcome in zip(wave, wave_results, strict=True):
                latest = await self._require_active_run(run.run_id)
                await self._writer.commit_agent_call_outcome(
                    AgentCallOutcomeCommit(
                        run_id=run.run_id,
                        expected_revision=latest.revision,
                        expected_claim_token=handle.current.token,
                        call_item_id=call_item.item_id,
                        safe_result_payload=outcome.safe_result_payload,
                        status=outcome.status,
                        staged_artifacts=outcome.staged_artifacts,
                        safe_error_code=outcome.safe_error_code,
                        skill_activation_item=outcome.skill_activation_item,
                    )
                )
            latest = await self._runs.get_run(run.run_id)
            if latest is None:
                raise AgentStorageConflict("agent_run_missing")
            if latest.status in {
                AgentRunStatus.WAITING_FOR_INPUT,
                AgentRunStatus.WAITING_FOR_DEPENDENCY,
            }:
                released = await self._leases.release_waiting(run.run_id, handle=handle)
                return AgentLoopRunResult(
                    run=released,
                    state="waiting",
                    lease_handle=handle,
                )
        return None

    async def _execute_wave(
        self,
        run: AgentRun,
        wave: tuple[tuple[AgentToolCall, AgentItem, AgentItem], ...],
        tool_to_capability: Mapping[str, str],
        policies: Mapping[str, CapabilityInvocationPolicy],
        cancellation: AgentCancellationToken | None,
        visibility: CapabilityVisibilityContext,
        lease_handle: AgentLeaseHandle,
    ) -> tuple[AgentCallExecution, ...]:
        async def execute(record):
            call, call_item, reservation = record
            capability_id = tool_to_capability.get(call.provider_safe_name)
            policy = policies.get(capability_id or "")
            if capability_id is None or policy is None:
                return AgentCallExecution(
                    AgentCallOutcomeStatus.FAILED,
                    safe_error_code="unknown_tool",
                )
            try:
                effective = policy.effective_payload(
                    json.loads(call.arguments_json),
                    context=visibility,
                )
            except (ValueError, json.JSONDecodeError):
                return AgentCallExecution(
                    AgentCallOutcomeStatus.FAILED,
                    safe_error_code="invalid_tool_arguments",
                )
            return await self._invoker.invoke(
                run=run,
                call=call,
                call_item=call_item,
                result_reservation=reservation,
                capability_id=capability_id,
                effective_payload=effective,
                cancellation=cancellation,
                lease_handle=lease_handle,
            )

        return tuple(await asyncio.gather(*(execute(record) for record in wave)))

    async def _require_active_run(self, run_id: str) -> AgentRun:
        run = await self._runs.get_run(run_id)
        if run is None:
            raise AgentStorageConflict("agent_run_missing")
        if run.status in {
            AgentRunStatus.COMPLETED,
            AgentRunStatus.FAILED,
            AgentRunStatus.CANCELLED,
        }:
            raise AgentStorageConflict("agent_run_terminal")
        return run

    @staticmethod
    def _check_cancel(cancellation: AgentCancellationToken | None) -> None:
        if cancellation is not None and cancellation.is_cancelled():
            raise asyncio.CancelledError


def _parallel_safe(
    call: AgentToolCall,
    tool_to_capability: Mapping[str, str],
    policies: Mapping[str, CapabilityInvocationPolicy],
) -> bool:
    capability_id = tool_to_capability.get(call.provider_safe_name)
    policy = policies.get(capability_id or "")
    return bool(policy is not None and policy.parallel_safe)


def _truncate_utf8(value: str, max_bytes: int) -> str:
    if max_bytes <= 0:
        return ""
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _pending_active_batch(
    run: AgentRun,
    items: tuple[AgentItem, ...],
) -> tuple[tuple[AgentToolCall, AgentItem, AgentItem], ...]:
    if run.active_sample_item_id is None:
        return ()
    calls = {
        item.item_id: item
        for item in items
        if item.kind is AgentItemKind.TOOL_CALL
        and item.parent_item_id == run.active_sample_item_id
    }
    reservations = {
        item.source_call_item_id: item
        for item in items
        if item.kind is AgentItemKind.TOOL_RESULT
        and item.state is AgentItemState.RESERVED
        and item.source_call_item_id in calls
    }
    records = []
    for call_item in sorted(
        calls.values(),
        key=lambda item: item.call_ordinal if item.call_ordinal is not None else -1,
    ):
        reservation = reservations.get(call_item.item_id)
        if reservation is None or call_item.item_id in run.waiting_call_item_ids:
            continue
        payload = json.loads(call_item.payload_json)
        records.append(
            (
                AgentToolCall(
                    call_id=str(payload["call_id"]),
                    provider_safe_name=str(payload["provider_safe_name"]),
                    arguments_json=str(payload["arguments_json"]),
                    ordinal=call_item.call_ordinal or 0,
                ),
                call_item,
                reservation,
            )
        )
    return tuple(records)
