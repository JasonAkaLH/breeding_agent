from __future__ import annotations

import inspect
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from src.orchestration.agent_loop.continuation import (
    AgentContinuationLocator,
    AgentContinuationLocatorService,
)
from src.orchestration.agent_loop.lease import AgentLeaseController, AgentLeaseHandle
from src.orchestration.agent_loop.models import (
    AgentCallOutcomeCommit,
    AgentCallOutcomeStatus,
    AgentItem,
    AgentItemKind,
    AgentItemState,
    AgentRun,
    AgentRunStatus,
    AgentStagedArtifact,
    AgentStorageConflict,
)
from src.orchestration.agent_loop.repository import (
    AgentAtomicWriter,
    AgentRunRepository,
    AgentTaskLeaseStore,
)


class AgentRecoveryState(StrEnum):
    WAITING = "waiting"
    RESUMED = "resumed"
    FINAL_CANDIDATE = "final_candidate"
    DUPLICATE = "duplicate"
    TERMINAL = "terminal"


@dataclass(frozen=True, slots=True)
class AgentAuthorityResolution:
    authority_digest: str
    status: AgentCallOutcomeStatus
    safe_result_payload: Any
    safe_continuation_facts: Mapping[str, Any]
    safe_error_code: str | None = None
    staged_artifacts: tuple[AgentStagedArtifact, ...] = ()


@dataclass(frozen=True, slots=True)
class AgentRecoveryResult:
    run: AgentRun
    state: AgentRecoveryState
    result_item: AgentItem | None = None
    acknowledged: bool = False
    loop_result: Any | None = None


@dataclass(frozen=True, slots=True)
class AgentTransientRecoveryOutcome:
    safe_result_payload: Mapping[str, Any]


class AgentClaimedRunResumer(Protocol):
    async def run_claimed(
        self,
        run_id: str,
        *,
        handle: AgentLeaseHandle,
        initial_required_tool_name: str | None = None,
        trusted_facts: tuple[str, ...] = (),
        visibility_context: Any | None = None,
        cancellation: Any | None = None,
    ) -> Any: ...


AuthorityResolver = Callable[
    [AgentContinuationLocator, AgentLeaseHandle],
    AgentAuthorityResolution | Awaitable[AgentAuthorityResolution],
]
Acknowledger = Callable[[], Any | Awaitable[Any]]
TransientResultRecoverer = Callable[
    [AgentRun, AgentItem, AgentItem],
    AgentTransientRecoveryOutcome
    | None
    | Awaitable[AgentTransientRecoveryOutcome | None],
]


class AgentRunRecoveryCoordinator:
    """Production continuation and crash-recovery coordinator for AgentRuns."""

    _TERMINAL = frozenset(
        {AgentRunStatus.COMPLETED, AgentRunStatus.FAILED, AgentRunStatus.CANCELLED}
    )
    _FORBIDDEN_SAFE_KEYS = frozenset(
        {
            "arguments",
            "arguments_json",
            "attachment_body",
            "content_base64",
            "credential",
            "credentials",
            "input_payload",
            "raw_result",
            "tool_arguments",
            "user_text",
        }
    )

    def __init__(
        self,
        *,
        runs: AgentRunRepository,
        writer: AgentAtomicWriter,
        lease_store: AgentTaskLeaseStore,
        resumer: AgentClaimedRunResumer,
        locator_service: AgentContinuationLocatorService | None = None,
        lease_ttl_seconds: float = 30,
        owner_id: str = "agent-recovery",
        transient_result_recoverer: TransientResultRecoverer | None = None,
    ) -> None:
        self._runs = runs
        self._writer = writer
        self._leases = AgentLeaseController(lease_store, ttl_seconds=lease_ttl_seconds)
        self._resumer = resumer
        self._locators = locator_service or AgentContinuationLocatorService()
        self._owner_id = owner_id
        self._recover_transient_result = transient_result_recoverer

    async def continue_waiting_call(
        self,
        locator: AgentContinuationLocator,
        *,
        owner_scope: str,
        authority_digest: str,
        resolve_authority: AuthorityResolver,
        acknowledge: Acknowledger | None = None,
    ) -> AgentRecoveryResult:
        return await self._close_call(
            locator,
            owner_scope=owner_scope,
            authority_digest=authority_digest,
            resolve_authority=resolve_authority,
            acknowledge=acknowledge,
            require_waiting=True,
        )

    async def recover_authoritative_result(
        self,
        locator: AgentContinuationLocator,
        *,
        owner_scope: str,
        authority_digest: str,
        resolve_authority: AuthorityResolver,
        acknowledge: Acknowledger | None = None,
    ) -> AgentRecoveryResult:
        return await self._close_call(
            locator,
            owner_scope=owner_scope,
            authority_digest=authority_digest,
            resolve_authority=resolve_authority,
            acknowledge=acknowledge,
            require_waiting=False,
        )

    async def converge_unknown_side_effect(
        self,
        locator: AgentContinuationLocator,
        *,
        owner_scope: str,
        authority_digest: str,
        safe_reason_code: str = "side_effect_unknown_no_replay",
    ) -> AgentRecoveryResult:
        async def no_replay(
            _locator: AgentContinuationLocator,
            _handle: AgentLeaseHandle,
        ) -> AgentAuthorityResolution:
            return AgentAuthorityResolution(
                authority_digest=authority_digest,
                status=AgentCallOutcomeStatus.ABORTED,
                safe_result_payload={"status": "aborted"},
                safe_continuation_facts={"recovery": "unknown_no_replay"},
                safe_error_code=safe_reason_code,
            )

        return await self._close_call(
            locator,
            owner_scope=owner_scope,
            authority_digest=authority_digest,
            resolve_authority=no_replay,
            acknowledge=None,
            require_waiting=False,
        )

    async def recover_crashed_run(
        self,
        run_id: str,
        *,
        initial_required_tool_name: str | None = None,
        trusted_facts: tuple[str, ...] = (),
        visibility_context: Any | None = None,
        cancellation: Any | None = None,
    ) -> AgentRecoveryResult:
        run = await self._writer.reconcile_agent_run_consistency(run_id)
        if run.status in self._TERMINAL:
            return AgentRecoveryResult(run, AgentRecoveryState.TERMINAL)
        if run.status in {
            AgentRunStatus.WAITING_FOR_INPUT,
            AgentRunStatus.WAITING_FOR_DEPENDENCY,
        }:
            return AgentRecoveryResult(run, AgentRecoveryState.WAITING)

        handle = await self._leases.acquire(run_id, owner_id=self._owner_id)
        run = await self._require_run(run_id)
        items = await self._runs.list_items(run_id)
        calls = {
            item.item_id: item
            for item in items
            if item.kind is AgentItemKind.TOOL_CALL
        }
        reservations = sorted(
            (
                item
                for item in items
                if item.kind is AgentItemKind.TOOL_RESULT
                and item.state is AgentItemState.RESERVED
                and item.source_call_item_id in calls
                and item.source_call_item_id not in run.waiting_call_item_ids
            ),
            key=lambda item: item.call_ordinal or 0,
        )
        for reservation in reservations:
            call_id = reservation.source_call_item_id
            if call_id is None:
                raise AgentStorageConflict("agent_recovery_call_identity_missing")
            latest = await self._require_run(run_id)
            recovered = None
            recovery_error = None
            if self._recover_transient_result is not None:
                try:
                    recovered = self._recover_transient_result(
                        latest,
                        calls[call_id],
                        reservation,
                    )
                    if inspect.isawaitable(recovered):
                        recovered = await recovered
                    if recovered is not None and not isinstance(
                        recovered, AgentTransientRecoveryOutcome
                    ):
                        raise ValueError(
                            "agent_transient_skill_result_unavailable"
                        )
                except ValueError as exc:
                    recovery_error = (
                        str(exc)
                        if str(exc)
                        in {
                            "agent_transient_skill_result_stage_failed",
                            "agent_transient_skill_result_unavailable",
                        }
                        else "agent_transient_skill_result_unavailable"
                    )
            await self._writer.commit_agent_call_outcome(
                AgentCallOutcomeCommit(
                    run_id=run_id,
                    expected_revision=latest.revision,
                    expected_claim_token=handle.current.token,
                    call_item_id=call_id,
                    safe_result_payload=(
                        recovered.safe_result_payload
                        if recovered is not None
                        else (
                            None
                            if recovery_error is not None
                            else {"status": "aborted"}
                        )
                    ),
                    status=(
                        AgentCallOutcomeStatus.COMPLETED
                        if recovered is not None
                        else AgentCallOutcomeStatus.FAILED
                        if recovery_error is not None
                        else AgentCallOutcomeStatus.ABORTED
                    ),
                    safe_error_code=(
                        recovery_error
                        or (
                            None
                            if recovered is not None
                            else "side_effect_unknown_no_replay"
                        )
                    ),
                )
            )

        loop_result = await self._resumer.run_claimed(
            run_id,
            handle=handle,
            initial_required_tool_name=(
                initial_required_tool_name
                if len(items) == 1
                and items[0].kind is AgentItemKind.USER_MESSAGE
                and items[0].state is AgentItemState.COMMITTED
                else None
            ),
            trusted_facts=trusted_facts,
            visibility_context=visibility_context,
            cancellation=cancellation,
        )
        state = (
            AgentRecoveryState.FINAL_CANDIDATE
            if getattr(loop_result, "state", None) == "final_candidate"
            else AgentRecoveryState.WAITING
            if getattr(loop_result, "state", None) == "waiting"
            else AgentRecoveryState.RESUMED
        )
        return AgentRecoveryResult(
            loop_result.run,
            state,
            loop_result=loop_result,
        )

    async def _require_run(self, run_id: str) -> AgentRun:
        run = await self._runs.get_run(run_id)
        if run is None:
            raise AgentStorageConflict("agent_recovery_run_missing")
        return run

    async def _close_call(
        self,
        locator: AgentContinuationLocator,
        *,
        owner_scope: str,
        authority_digest: str,
        resolve_authority: AuthorityResolver,
        acknowledge: Acknowledger | None,
        require_waiting: bool,
    ) -> AgentRecoveryResult:
        run, call, result = await self._load_and_validate(
            locator,
            owner_scope=owner_scope,
            authority_digest=authority_digest,
            require_waiting=require_waiting,
        )
        if result.state is AgentItemState.COMMITTED:
            acknowledged = await _acknowledge(acknowledge)
            state = (
                AgentRecoveryState.TERMINAL
                if run.status in self._TERMINAL
                else AgentRecoveryState.DUPLICATE
            )
            return AgentRecoveryResult(run, state, result, acknowledged)
        if run.status in self._TERMINAL:
            acknowledged = await _acknowledge(acknowledge)
            return AgentRecoveryResult(run, AgentRecoveryState.TERMINAL, None, acknowledged)

        handle = await self._leases.acquire(run.run_id, owner_id=self._owner_id)
        run, call, result = await self._load_and_validate(
            locator,
            owner_scope=owner_scope,
            authority_digest=authority_digest,
            require_waiting=require_waiting,
        )
        if result.state is AgentItemState.COMMITTED:
            acknowledged = await _acknowledge(acknowledge)
            return AgentRecoveryResult(run, AgentRecoveryState.DUPLICATE, result, acknowledged)

        resolution = await self._leases.run_active_phase(
            "capability_wave",
            handle,
            lambda current_handle: _resolve(
                resolve_authority,
                locator,
                current_handle,
            ),
        )
        self._validate_resolution(locator, resolution, authority_digest)
        run, call, result = await self._load_and_validate(
            locator,
            owner_scope=owner_scope,
            authority_digest=authority_digest,
            require_waiting=require_waiting,
        )
        if result.state is AgentItemState.COMMITTED:
            acknowledged = await _acknowledge(acknowledge)
            return AgentRecoveryResult(run, AgentRecoveryState.DUPLICATE, result, acknowledged)
        if run.status in self._TERMINAL:
            acknowledged = await _acknowledge(acknowledge)
            return AgentRecoveryResult(run, AgentRecoveryState.TERMINAL, None, acknowledged)
        committed = await self._writer.commit_agent_call_outcome(
            AgentCallOutcomeCommit(
                run_id=run.run_id,
                expected_revision=run.revision,
                expected_claim_token=handle.current.token,
                call_item_id=call.item_id,
                safe_result_payload=resolution.safe_result_payload,
                status=resolution.status,
                continuation_payload={
                    "authority_digest": resolution.authority_digest,
                    "facts": dict(resolution.safe_continuation_facts),
                    "locator_digest": locator.digest,
                    "resume_kind": locator.resume_kind.value,
                    "schema": "maf.agent.continuation.v1",
                },
                staged_artifacts=resolution.staged_artifacts,
                safe_error_code=resolution.safe_error_code,
            )
        )
        acknowledged = await _acknowledge(acknowledge)
        updated = await self._runs.get_run(run.run_id)
        if updated is None:
            raise AgentStorageConflict("agent_run_missing_after_continuation")
        if updated.status in {
            AgentRunStatus.WAITING_FOR_INPUT,
            AgentRunStatus.WAITING_FOR_DEPENDENCY,
        }:
            released = await self._leases.release_waiting(run.run_id, handle=handle)
            return AgentRecoveryResult(
                released,
                AgentRecoveryState.WAITING,
                committed,
                acknowledged,
            )
        loop_result = await self._resumer.run_claimed(run.run_id, handle=handle)
        loop_state = getattr(loop_result, "state", None)
        if loop_state == "final_candidate":
            resumed_state = AgentRecoveryState.FINAL_CANDIDATE
        elif loop_state == "waiting":
            resumed_state = AgentRecoveryState.WAITING
        else:
            resumed_state = AgentRecoveryState.RESUMED
        return AgentRecoveryResult(
            loop_result.run,
            resumed_state,
            committed,
            acknowledged,
            loop_result,
        )

    async def _load_and_validate(
        self,
        locator: AgentContinuationLocator,
        *,
        owner_scope: str,
        authority_digest: str,
        require_waiting: bool,
    ) -> tuple[AgentRun, AgentItem, AgentItem]:
        run = await self._runs.get_run(locator.run_id)
        if run is None:
            raise AgentStorageConflict("agent_continuation_run_missing")
        items = await self._runs.list_items(run.run_id)
        call = next(
            (
                item
                for item in items
                if item.item_id == locator.call_item_id
                and item.kind is AgentItemKind.TOOL_CALL
            ),
            None,
        )
        result = next(
            (item for item in items if item.source_call_item_id == locator.call_item_id),
            None,
        )
        if call is None or result is None:
            raise AgentStorageConflict("agent_continuation_call_result_missing")
        self._locators.validate_identity(
            locator,
            run=run,
            call_item=call,
            owner_scope=owner_scope,
            authority_digest=authority_digest,
        )
        if (
            require_waiting
            and run.status not in self._TERMINAL
            and result.state is AgentItemState.RESERVED
            and call.item_id not in run.waiting_call_item_ids
        ):
            raise AgentStorageConflict("agent_continuation_call_not_waiting")
        return run, call, result

    def _validate_resolution(
        self,
        locator: AgentContinuationLocator,
        resolution: AgentAuthorityResolution,
        authority_digest: str,
    ) -> None:
        if (
            not isinstance(resolution, AgentAuthorityResolution)
            or resolution.authority_digest != authority_digest
            or resolution.authority_digest != locator.authority_digest
            or resolution.status
            not in {
                AgentCallOutcomeStatus.COMPLETED,
                AgentCallOutcomeStatus.FAILED,
                AgentCallOutcomeStatus.ABORTED,
                AgentCallOutcomeStatus.WAITING_FOR_INPUT,
                AgentCallOutcomeStatus.WAITING_FOR_DEPENDENCY,
            }
            or (
                resolution.safe_error_code is not None
                and re.fullmatch(
                    r"[a-z0-9][a-z0-9_.-]{0,127}",
                    resolution.safe_error_code,
                )
                is None
            )
        ):
            raise AgentStorageConflict("agent_continuation_authority_invalid")
        _reject_unsafe_keys(resolution.safe_result_payload, self._FORBIDDEN_SAFE_KEYS)
        _reject_unsafe_keys(resolution.safe_continuation_facts, self._FORBIDDEN_SAFE_KEYS)


async def _resolve(
    resolver: AuthorityResolver,
    locator: AgentContinuationLocator,
    handle: AgentLeaseHandle,
) -> AgentAuthorityResolution:
    value = resolver(locator, handle)
    resolved = await value if inspect.isawaitable(value) else value
    if not isinstance(resolved, AgentAuthorityResolution):
        raise AgentStorageConflict("agent_continuation_authority_invalid")
    return resolved


async def _acknowledge(acknowledge: Acknowledger | None) -> bool:
    if acknowledge is None:
        return False
    try:
        value = acknowledge()
        if inspect.isawaitable(value):
            await value
    except Exception:
        return False
    return True


def _reject_unsafe_keys(value: Any, forbidden: frozenset[str]) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if str(key).lower() in forbidden:
                raise AgentStorageConflict("agent_continuation_payload_unsafe")
            _reject_unsafe_keys(nested, forbidden)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _reject_unsafe_keys(nested, forbidden)
