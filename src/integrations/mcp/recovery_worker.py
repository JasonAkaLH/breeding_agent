from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
from uuid import uuid4

from src.core.models import MCPRemoteTaskBinding, MCPRemoteTaskOutbox, Task

from .adapter_2025_tasks import (
    MCP2025TaskCancelAck,
    MCP2025TaskResult,
    MCP2025TaskState,
)
from .adapter_2026 import MCPTaskState
from .credentials import MCPRecoveryCallContext
from .protocol import (
    MCP_PROTOCOL_VERSION_2025_11_25,
    MCP_PROTOCOL_VERSION_2026_07_28,
)
from .rollout_evidence import MCPMetricErrorCategory, MCPMetricResultCategory
from .rollout_evidence import MCPSafetyRedLine
from .safety_detectors import AuthoritativeMCPSafetyDetector


REMOTE_TASK_CLAIM_TTL_SECONDS = 30.0
REMOTE_TASK_CLAIM_RENEW_SECONDS = 10.0
REMOTE_TASK_IDLE_POLL_SECONDS = 1.0
REMOTE_TASK_ERROR_BACKOFF_SECONDS = 30.0
REMOTE_TASK_DEFAULT_POLL_MS = 1_000


class MCPRemoteTaskRecoveryError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class MCPRemoteTaskStorage(Protocol):
    async def get_task(self, task_id: str) -> Task | None: ...
    async def get_mcp_remote_task_binding(
        self, owner_user_id: str, task_id: str, safe_remote_task_ref: str
    ) -> MCPRemoteTaskBinding | None: ...
    async def claim_due_mcp_remote_task_bindings(
        self,
        *,
        claim_owner: str,
        claim_token: str,
        now: datetime,
        lease_expires_at: datetime,
        limit: int = 100,
    ) -> list[MCPRemoteTaskBinding]: ...

    async def renew_mcp_remote_task_binding_claim(
        self,
        owner_user_id: str,
        task_id: str,
        safe_remote_task_ref: str,
        *,
        claim_owner: str,
        claim_token: str,
        expected_revision: int,
        lease_expires_at: datetime,
        updated_at: datetime,
    ) -> MCPRemoteTaskBinding | None: ...

    async def claim_mcp_remote_task_outbox(
        self, *, claim_owner: str, claim_token: str, now: datetime,
        lease_expires_at: datetime, limit: int = 100,
    ) -> list[MCPRemoteTaskOutbox]: ...

    async def claim_abandoned_mcp_remote_task_controls(
        self, *, claim_owner: str, claim_token: str, now: datetime,
        limit: int = 100,
    ) -> list[MCPRemoteTaskOutbox]: ...

    async def apply_mcp_remote_task_continuation(
        self, outbox_id: str, *, claim_owner: str, claim_token: str,
        expected_revision: int, updated_at: datetime,
    ) -> MCPRemoteTaskOutbox | None: ...

    async def admit_mcp_remote_task_continuation(
        self, outbox_id: str, *, claim_owner: str, claim_token: str,
        expected_revision: int, admitted_at: datetime,
    ) -> MCPRemoteTaskOutbox | None: ...

    async def mark_mcp_remote_task_continuation_dispatched(
        self, outbox_id: str, *, claim_owner: str, claim_token: str,
        expected_revision: int, dispatched_at: datetime,
    ) -> MCPRemoteTaskOutbox | None: ...

    async def begin_mcp_remote_task_control_delivery(
        self, outbox_id: str, *, claim_owner: str, claim_token: str,
        expected_revision: int, lease_expires_at: datetime, updated_at: datetime,
    ) -> MCPRemoteTaskOutbox | None: ...

    async def complete_mcp_remote_task_outbox(
        self, outbox_id: str, *, claim_owner: str, claim_token: str,
        expected_revision: int, completed_at: datetime,
    ) -> MCPRemoteTaskOutbox | None: ...

    async def complete_mcp_remote_task_control(
        self, outbox_id: str, *, claim_owner: str, claim_token: str,
        expected_revision: int, outcome: str, completed_at: datetime,
    ) -> MCPRemoteTaskOutbox | None: ...

    async def pause_mcp_remote_task_for_input(
        self, owner_user_id: str, task_id: str, safe_remote_task_ref: str, *,
        claim_owner: str, claim_token: str, expected_revision: int,
        input_requests: Mapping[str, Any], conversation_id: str,
        source_message_id: str, updated_at: datetime,
    ) -> MCPRemoteTaskBinding | None: ...

    async def release_mcp_remote_task_binding_claim(
        self,
        owner_user_id: str,
        task_id: str,
        safe_remote_task_ref: str,
        *,
        claim_owner: str,
        claim_token: str,
        expected_revision: int,
        updated_at: datetime,
    ) -> MCPRemoteTaskBinding | None: ...

    async def update_mcp_remote_task_binding_status(
        self,
        owner_user_id: str,
        task_id: str,
        safe_remote_task_ref: str,
        *,
        claim_owner: str,
        claim_token: str,
        expected_revision: int,
        last_status: str,
        next_poll_at: datetime | None,
        updated_at: datetime,
        terminal_at: datetime | None = None,
    ) -> MCPRemoteTaskBinding | None: ...

    async def finish_mcp_remote_task_binding(
        self,
        owner_user_id: str,
        task_id: str,
        safe_remote_task_ref: str,
        *,
        claim_owner: str,
        claim_token: str,
        expected_revision: int,
        remote_status: str,
        call_status: str,
        terminal_at: datetime,
        result_ref: str | None = None,
        safe_error_code: str | None = None,
        result_receipt_id: str | None = None,
    ) -> MCPRemoteTaskBinding | None: ...


@dataclass(frozen=True, slots=True)
class MCPRemoteTaskPollResult:
    status: str
    terminal: bool
    poll_interval_ms: int | None = None
    final_result: Mapping[str, Any] | None = None
    input_requests: Mapping[str, Any] | None = None


class MCPRemoteTaskProtocolHandler(Protocol):
    protocol_version: str

    async def poll(
        self, client: Any, binding: MCPRemoteTaskBinding
    ) -> MCPRemoteTaskPollResult: ...


class MCP2026RemoteTaskProtocolHandler:
    protocol_version = MCP_PROTOCOL_VERSION_2026_07_28

    async def poll(
        self, client: Any, binding: MCPRemoteTaskBinding
    ) -> MCPRemoteTaskPollResult:
        state = await client.tasks_get(
            binding.safe_remote_task_ref,
            recovery_context=MCPRecoveryCallContext(
                owner_user_id=binding.owner_user_id,
                task_id=binding.task_id,
                node_id=binding.node_id,
                call_ref=binding.call_ref,
            ),
        )
        if not isinstance(state, MCPTaskState):
            raise MCPRemoteTaskRecoveryError("mcp_remote_task_response_invalid")
        return MCPRemoteTaskPollResult(
            status=state.status,
            terminal=state.terminal,
            poll_interval_ms=state.poll_interval_ms,
            final_result=(state.result or {}) if state.status == "completed" else state.result,
            input_requests=state.input_requests,
        )

    async def submit_input(
        self,
        client: Any,
        binding: MCPRemoteTaskBinding,
        input_responses: Mapping[str, Any],
    ) -> None:
        await client.tasks_update(
            binding.safe_remote_task_ref,
            input_responses,
            recovery_context=MCPRecoveryCallContext(
                owner_user_id=binding.owner_user_id,
                task_id=binding.task_id,
                node_id=binding.node_id,
                call_ref=binding.call_ref,
            ),
        )

    async def cancel(
        self, client: Any, binding: MCPRemoteTaskBinding, *, reason: str
    ) -> bool:
        await client.tasks_cancel(
            binding.safe_remote_task_ref,
            recovery_context=MCPRecoveryCallContext(
                owner_user_id=binding.owner_user_id,
                task_id=binding.task_id,
                node_id=binding.node_id,
                call_ref=binding.call_ref,
            ),
            reason=reason,
        )
        return True


class MCP2025RemoteTaskProtocolHandler:
    protocol_version = MCP_PROTOCOL_VERSION_2025_11_25

    async def poll(
        self, client: Any, binding: MCPRemoteTaskBinding
    ) -> MCPRemoteTaskPollResult:
        context = MCPRecoveryCallContext(
            owner_user_id=binding.owner_user_id,
            task_id=binding.task_id,
            node_id=binding.node_id,
            call_ref=binding.call_ref,
        )
        state = await client.tasks_get(
            binding.safe_remote_task_ref,
            recovery_context=context,
        )
        if not isinstance(state, MCP2025TaskState):
            raise MCPRemoteTaskRecoveryError("mcp_remote_task_response_invalid")
        final_result = None
        if state.status == "completed":
            task_result = await client.tasks_result(
                binding.safe_remote_task_ref,
                recovery_context=context,
            )
            if not isinstance(task_result, MCP2025TaskResult):
                raise MCPRemoteTaskRecoveryError(
                    "mcp_remote_task_result_response_invalid"
                )
            final_result = task_result.call_tool_result
        return MCPRemoteTaskPollResult(
            status=state.status,
            terminal=state.terminal,
            poll_interval_ms=state.poll_interval_ms,
            final_result=final_result,
        )

    async def cancel(
        self,
        client: Any,
        binding: MCPRemoteTaskBinding,
        *,
        reason: str,
    ) -> bool:
        acknowledgement = await client.tasks_cancel(
            binding.safe_remote_task_ref,
            recovery_context=MCPRecoveryCallContext(
                owner_user_id=binding.owner_user_id,
                task_id=binding.task_id,
                node_id=binding.node_id,
                call_ref=binding.call_ref,
            ),
            reason=reason,
        )
        if not isinstance(acknowledgement, MCP2025TaskCancelAck):
            raise MCPRemoteTaskRecoveryError("mcp_remote_task_cancel_response_invalid")
        return acknowledgement.cancelled


RemoteTaskClientFactory = Callable[[MCPRemoteTaskBinding], Any | Awaitable[Any]]
RemoteTaskEventSink = Callable[[MCPRemoteTaskBinding, str], Any | Awaitable[Any]]
RemoteTaskMetricGapSink = Callable[[MCPRemoteTaskBinding, str], Any | Awaitable[Any]]
RemoteTaskActiveMetricSink = Callable[[], Any | Awaitable[Any]]
RemoteTaskGlobalMetricGapSink = Callable[[str], Any | Awaitable[Any]]
RemoteTaskResultPersister = Callable[
    [MCPRemoteTaskBinding, Mapping[str, Any]], str | Awaitable[str]
]
RemoteTaskResultCommitter = Callable[
    [MCPRemoteTaskBinding, str | None], str | None | Awaitable[str | None]
]
RemoteTaskTerminalSealer = Callable[
    [MCPRemoteTaskBinding, str, str | None, str | None], Any | Awaitable[Any]
]
RemoteTaskContinuationSink = Callable[
    [MCPRemoteTaskOutbox], Any | Awaitable[Any]
]
NowFn = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class MCPRemoteTaskTerminalMetricSample:
    """Safe terminal sample emitted only after durable atomic convergence."""

    binding: MCPRemoteTaskBinding
    result_category: MCPMetricResultCategory
    error_category: MCPMetricErrorCategory
    duration_seconds: float
    terminal_at: datetime


@dataclass(frozen=True, slots=True)
class MCPContinuationAdmissionResult:
    status: str
    outbox: MCPRemoteTaskOutbox

    def __post_init__(self) -> None:
        if self.status not in {"admitted_new", "already_admitted", "completed"}:
            raise ValueError("invalid MCP continuation admission status")


RemoteTaskTerminalMetricSink = Callable[
    [MCPRemoteTaskTerminalMetricSample], Any | Awaitable[Any]
]


class MCPRemoteTaskRecoveryWorker:
    """Claims durable bindings and performs query-only remote Task recovery."""

    def __init__(
        self,
        *,
        storage: MCPRemoteTaskStorage,
        client_factory: RemoteTaskClientFactory,
        instance_id: str,
        handlers: Mapping[str, MCPRemoteTaskProtocolHandler] | None = None,
        event_sink: RemoteTaskEventSink | None = None,
        terminal_metric_sink: RemoteTaskTerminalMetricSink | None = None,
        metric_gap_sink: RemoteTaskMetricGapSink | None = None,
        active_metric_sink: RemoteTaskActiveMetricSink | None = None,
        global_metric_gap_sink: RemoteTaskGlobalMetricGapSink | None = None,
        result_persister: RemoteTaskResultPersister | None = None,
        result_committer: RemoteTaskResultCommitter | None = None,
        terminal_sealer: RemoteTaskTerminalSealer | None = None,
        continuation_sink: RemoteTaskContinuationSink | None = None,
        now_fn: NowFn | None = None,
        claim_ttl_seconds: float = REMOTE_TASK_CLAIM_TTL_SECONDS,
        claim_renew_seconds: float = REMOTE_TASK_CLAIM_RENEW_SECONDS,
        idle_poll_seconds: float = REMOTE_TASK_IDLE_POLL_SECONDS,
        error_backoff_seconds: float = REMOTE_TASK_ERROR_BACKOFF_SECONDS,
        batch_size: int = 100,
    ) -> None:
        if not instance_id:
            raise ValueError("instance_id is required")
        if claim_ttl_seconds <= 0 or claim_renew_seconds <= 0:
            raise ValueError("remote task claim durations must be positive")
        if claim_renew_seconds >= claim_ttl_seconds:
            raise ValueError("remote task claim renewal must be shorter than its TTL")
        if idle_poll_seconds <= 0 or error_backoff_seconds <= 0 or batch_size < 1:
            raise ValueError(
                "remote task worker timing and batch size must be positive"
            )
        resolved_handlers: Mapping[str, MCPRemoteTaskProtocolHandler]
        if handlers is None:
            resolved_handlers = {
                MCP_PROTOCOL_VERSION_2025_11_25: MCP2025RemoteTaskProtocolHandler(),
                MCP_PROTOCOL_VERSION_2026_07_28: MCP2026RemoteTaskProtocolHandler(),
            }
        else:
            resolved_handlers = handlers
        if any(
            version != handler.protocol_version
            for version, handler in resolved_handlers.items()
        ):
            raise ValueError("remote task handler version mapping is inconsistent")
        self._storage = storage
        self._client_factory = client_factory
        self._instance_id = instance_id
        self._handlers = dict(resolved_handlers)
        self._event_sink = event_sink
        self._terminal_metric_sink = terminal_metric_sink
        self._metric_gap_sink = metric_gap_sink
        self._active_metric_sink = active_metric_sink
        self._global_metric_gap_sink = global_metric_gap_sink
        self._result_persister = result_persister
        self._result_committer = result_committer
        self._terminal_sealer = terminal_sealer
        self._continuation_sink = continuation_sink
        self._now = now_fn or (lambda: datetime.now(timezone.utc).replace(tzinfo=None))
        self._claim_ttl = timedelta(seconds=claim_ttl_seconds)
        self._claim_renew_seconds = claim_renew_seconds
        self._idle_poll_seconds = idle_poll_seconds
        self._error_backoff = timedelta(seconds=error_backoff_seconds)
        self._batch_size = batch_size
        self._stop = asyncio.Event()
        self._runner: asyncio.Task[None] | None = None
        self._active: dict[str, tuple[MCPRemoteTaskBinding, str]] = {}
        self._safety_detectors: dict[
            MCPSafetyRedLine, AuthoritativeMCPSafetyDetector
        ] = {}

    def configure_safety_detectors(
        self,
        detectors: Mapping[MCPSafetyRedLine, AuthoritativeMCPSafetyDetector],
    ) -> None:
        self._safety_detectors = dict(detectors)

    def attest_safety_interval(
        self, bucket_started_at: datetime, bucket_ended_at: datetime
    ) -> None:
        del bucket_started_at, bucket_ended_at

    async def start(self) -> None:
        if self._runner is not None and not self._runner.done():
            return
        self._stop.clear()
        self._runner = asyncio.create_task(
            self.run_forever(), name=f"mcp-remote-task-recovery:{self._instance_id}"
        )

    async def run_forever(self) -> None:
        while not self._stop.is_set():
            delay = self._idle_poll_seconds
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                delay = self._error_backoff.total_seconds()
                await self._record_global_metric_gap(
                    "mcp_remote_task_worker_run_failed"
                )
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=delay
                )
            except TimeoutError:
                pass

    async def run_once(self) -> int:
        try:
            now = self._now()
            claim_token = f"mcp-recovery-claim-{uuid4().hex}"
            claimed = await self._storage.claim_due_mcp_remote_task_bindings(
                claim_owner=self._instance_id,
                claim_token=claim_token,
                now=now,
                lease_expires_at=now + self._claim_ttl,
                limit=self._batch_size,
            )
            if claimed:
                outcomes = await asyncio.gather(
                    *(self._process(binding, claim_token) for binding in claimed),
                    return_exceptions=True,
                )
                failures = [
                    outcome for outcome in outcomes if isinstance(outcome, BaseException)
                ]
                if failures:
                    raise failures[0]
            await self._consume_continuation_outbox(now)
            return len(claimed)
        finally:
            await self._record_active_metric()

    async def _consume_continuation_outbox(self, now: datetime) -> None:
        now = self._now()
        claim_token = f"mcp-continuation-claim-{uuid4().hex}"
        abandoned = await self._storage.claim_abandoned_mcp_remote_task_controls(
            claim_owner=self._instance_id,
            claim_token=claim_token,
            now=now,
            limit=self._batch_size,
        )
        for item in abandoned:
            completed = await self._storage.complete_mcp_remote_task_control(
                item.outbox_id,
                claim_owner=self._instance_id,
                claim_token=claim_token,
                expected_revision=item.revision,
                outcome="ambiguous",
                completed_at=self._now(),
            )
            if completed is None:
                raise MCPRemoteTaskRecoveryError(
                    "mcp_remote_task_control_abandonment_claim_lost"
                )
        if self._continuation_sink is None:
            return
        items = await self._storage.claim_mcp_remote_task_outbox(
            claim_owner=self._instance_id,
            claim_token=claim_token,
            now=now,
            lease_expires_at=now + self._claim_ttl,
            limit=self._batch_size,
        )
        for item in items:
            if item.kind in {"control_update", "control_cancel"}:
                await self._deliver_control(item, claim_token)
                continue
            applied = await self._storage.apply_mcp_remote_task_continuation(
                item.outbox_id,
                claim_owner=self._instance_id,
                claim_token=claim_token,
                expected_revision=item.revision,
                updated_at=now,
            )
            if applied is None:
                raise MCPRemoteTaskRecoveryError("mcp_continuation_claim_lost")
            admission = await _await_maybe(self._continuation_sink(applied))
            if not isinstance(admission, MCPContinuationAdmissionResult):
                raise MCPRemoteTaskRecoveryError(
                    "mcp_continuation_sink_admission_contract_invalid"
                )
            dispatched = admission.outbox
            if (
                dispatched.outbox_id != applied.outbox_id
                or dispatched.continuation_admitted_at is None
                or dispatched.continuation_status not in {"pending", "claimed", "running", "completed"}
            ):
                raise MCPRemoteTaskRecoveryError(
                    "mcp_continuation_sink_admission_incomplete"
                )
            completed = await self._storage.complete_mcp_remote_task_outbox(
                dispatched.outbox_id,
                claim_owner=self._instance_id,
                claim_token=claim_token,
                expected_revision=dispatched.revision,
                completed_at=self._now(),
            )
            if completed is None:
                raise MCPRemoteTaskRecoveryError("mcp_continuation_completion_lost")

    async def _deliver_control(
        self, item: MCPRemoteTaskOutbox, claim_token: str
    ) -> None:
        sending_at = self._now()
        sending = await self._storage.begin_mcp_remote_task_control_delivery(
            item.outbox_id,
            claim_owner=self._instance_id,
            claim_token=claim_token,
            expected_revision=item.revision,
            lease_expires_at=sending_at + self._claim_ttl,
            updated_at=sending_at,
        )
        if sending is None:
            raise MCPRemoteTaskRecoveryError("mcp_remote_task_control_claim_lost")
        item = sending
        binding = await self._storage.get_mcp_remote_task_binding(
            item.owner_user_id, item.task_id, item.safe_remote_task_ref
        )
        if binding is None:
            raise MCPRemoteTaskRecoveryError("mcp_remote_task_binding_missing")
        handler = self._handlers.get(binding.protocol_version)
        if handler is None:
            raise MCPRemoteTaskRecoveryError(
                "mcp_remote_task_protocol_handler_unavailable"
            )
        client = None
        outcome = "delivered"
        try:
            client = await _await_maybe(self._client_factory(binding))
            if item.kind == "control_update":
                submit_input = getattr(handler, "submit_input", None)
                if not callable(submit_input):
                    raise MCPRemoteTaskRecoveryError(
                        "mcp_remote_task_input_required_unsupported"
                    )
                responses = item.payload.get("input_responses")
                if not isinstance(responses, Mapping):
                    raise MCPRemoteTaskRecoveryError(
                        "mcp_remote_task_input_response_invalid"
                    )
                await _await_maybe(submit_input(client, binding, responses))
            else:
                cancel = getattr(handler, "cancel", None)
                if not callable(cancel):
                    raise MCPRemoteTaskRecoveryError(
                        "mcp_remote_task_cancel_unsupported"
                    )
                await _await_maybe(
                    cancel(
                        client,
                        binding,
                        reason=str(item.payload.get("reason") or ""),
                    )
                )
        except BaseException:
            outcome = "ambiguous"
        finally:
            if client is not None:
                await _safe_close(client)
        completed = await self._storage.complete_mcp_remote_task_control(
            item.outbox_id,
            claim_owner=self._instance_id,
            claim_token=claim_token,
            expected_revision=item.revision,
            outcome=outcome,
            completed_at=self._now(),
        )
        if completed is None:
            raise MCPRemoteTaskRecoveryError("mcp_remote_task_control_claim_lost")

    async def aclose(self) -> None:
        self._stop.set()
        runner = self._runner
        if runner is not None and runner is not asyncio.current_task():
            try:
                await asyncio.wait_for(
                    runner, timeout=max(1.0, self._claim_renew_seconds)
                )
            except TimeoutError:
                runner.cancel()
                await asyncio.gather(runner, return_exceptions=True)
        self._runner = None
        active = tuple(self._active.values())
        for binding, claim_token in active:
            await self._release(binding, claim_token)
        await self._record_active_metric()

    async def cancel_remote_task(
        self,
        binding: MCPRemoteTaskBinding,
        reason: str,
    ) -> bool | None:
        """Issue a version-allowed cooperative cancellation without replaying work."""

        handler = self._handlers.get(binding.protocol_version)
        cancel = getattr(handler, "cancel", None)
        if not callable(cancel) or binding.terminal_at is not None:
            return None
        client = None
        try:
            client = await _await_maybe(self._client_factory(binding))
            return bool(await _await_maybe(cancel(client, binding, reason=reason)))
        finally:
            if client is not None:
                await _safe_close(client)

    async def _process(self, binding: MCPRemoteTaskBinding, claim_token: str) -> None:
        self._active[binding.safe_remote_task_ref] = (binding, claim_token)
        revision = {"value": binding.revision}
        renew_stop = asyncio.Event()
        renewer = asyncio.create_task(
            self._renew_claim(binding, claim_token, revision, renew_stop),
            name=f"mcp-remote-task-renew:{binding.safe_remote_task_ref}",
        )
        client = None
        try:
            handler = self._handlers.get(binding.protocol_version)
            if handler is None:
                raise MCPRemoteTaskRecoveryError(
                    "mcp_remote_task_protocol_handler_unavailable"
                )
            client = await _await_maybe(self._client_factory(binding))
            result = await handler.poll(client, binding)
            await _stop_renewer(renew_stop, renewer)
            now = self._now()
            authoritative_binding = replace(
                binding,
                claim_owner=self._instance_id,
                claim_token=claim_token,
                revision=revision["value"],
            )
            next_poll_at = None
            if not result.terminal and result.status != "input_required":
                next_poll_at = now + timedelta(
                    milliseconds=result.poll_interval_ms or REMOTE_TASK_DEFAULT_POLL_MS
                )
            if result.terminal:
                remote_status, call_status, safe_error_code = _terminal_statuses(
                    result.status
                )
                result_ref = None
                result_receipt_id = None
                if call_status == "completed":
                    if result.final_result is None or self._result_persister is None:
                        raise MCPRemoteTaskRecoveryError(
                            "mcp_remote_task_result_persistence_unavailable"
                        )
                    result_ref = str(
                        await _await_maybe(
                            self._result_persister(
                                authoritative_binding, result.final_result
                            )
                        )
                    ).strip()
                    if not result_ref or result_ref == binding.safe_remote_task_ref:
                        raise MCPRemoteTaskRecoveryError(
                            "mcp_remote_task_result_persistence_failed"
                        )
                if self._terminal_sealer is not None:
                    await _await_maybe(
                        self._terminal_sealer(
                            authoritative_binding,
                            call_status,
                            result_ref,
                            safe_error_code,
                        )
                    )
                if self._result_committer is not None:
                    committed = await _await_maybe(
                        self._result_committer(authoritative_binding, result_ref)
                    )
                    result_receipt_id = (
                        None if committed is None else str(committed).strip()
                    )
                finish_kwargs = {
                    "claim_owner": self._instance_id,
                    "claim_token": claim_token,
                    "expected_revision": revision["value"],
                    "remote_status": remote_status,
                    "call_status": call_status,
                    "terminal_at": now,
                    "result_ref": result_ref,
                    "safe_error_code": safe_error_code,
                }
                if result_receipt_id:
                    finish_kwargs["result_receipt_id"] = result_receipt_id
                updated = await self._storage.finish_mcp_remote_task_binding(
                    binding.owner_user_id,
                    binding.task_id,
                    binding.safe_remote_task_ref,
                    **finish_kwargs,
                )
            elif result.status == "input_required":
                if binding.protocol_version == MCP_PROTOCOL_VERSION_2025_11_25:
                    safe_error = "mcp_remote_task_input_required_unsupported"
                    if self._terminal_sealer is not None:
                        await _await_maybe(
                            self._terminal_sealer(
                                authoritative_binding, "failed", None, safe_error
                            )
                        )
                    result_receipt_id = None
                    if self._result_committer is not None:
                        committed = await _await_maybe(
                            self._result_committer(authoritative_binding, None)
                        )
                        result_receipt_id = (
                            None if committed is None else str(committed).strip()
                        )
                    finish_kwargs = {
                        "claim_owner": self._instance_id,
                        "claim_token": claim_token,
                        "expected_revision": revision["value"],
                        "remote_status": "input_required",
                        "call_status": "failed",
                        "terminal_at": now,
                        "safe_error_code": safe_error,
                    }
                    if result_receipt_id:
                        finish_kwargs["result_receipt_id"] = result_receipt_id
                    updated = await self._storage.finish_mcp_remote_task_binding(
                        binding.owner_user_id,
                        binding.task_id,
                        binding.safe_remote_task_ref,
                        **finish_kwargs,
                    )
                    result = MCPRemoteTaskPollResult(
                        status="failed", terminal=True
                    )
                    call_status = "failed"
                else:
                    task = await self._storage.get_task(binding.task_id)
                    if task is None or result.input_requests is None:
                        raise MCPRemoteTaskRecoveryError(
                            "mcp_remote_task_input_required_invalid"
                        )
                    updated = await self._storage.pause_mcp_remote_task_for_input(
                        binding.owner_user_id,
                        binding.task_id,
                        binding.safe_remote_task_ref,
                        claim_owner=self._instance_id,
                        claim_token=claim_token,
                        expected_revision=revision["value"],
                        input_requests=result.input_requests,
                        conversation_id=task.conversation_id,
                        source_message_id=task.root_message_id,
                        updated_at=now,
                    )
            else:
                updated = await self._storage.update_mcp_remote_task_binding_status(
                    binding.owner_user_id,
                    binding.task_id,
                    binding.safe_remote_task_ref,
                    claim_owner=self._instance_id,
                    claim_token=claim_token,
                    expected_revision=revision["value"],
                    last_status=result.status,
                    next_poll_at=next_poll_at,
                    updated_at=now,
                )
            if updated is None:
                raise MCPRemoteTaskRecoveryError("mcp_remote_task_claim_lost")
            revision["value"] = updated.revision
            self._active[binding.safe_remote_task_ref] = (updated, claim_token)
            if result.terminal:
                await self._record_terminal_metric(
                    updated,
                    call_status=call_status,
                    terminal_at=now,
                )
            await self._emit(updated, updated.last_status)
            if not result.terminal and result.status != "input_required":
                await self._release(updated, claim_token)
        except asyncio.CancelledError:
            await _stop_renewer(renew_stop, renewer, propagate=False)
            active_entry = self._active.get(
                binding.safe_remote_task_ref, (binding, claim_token)
            )
            active_binding = active_entry[0]
            try:
                await self._release(active_binding, claim_token)
            finally:
                raise
        except Exception as exc:
            await _stop_renewer(renew_stop, renewer)
            if isinstance(exc, MCPRemoteTaskRecoveryError) and exc.code in {
                "mcp_remote_task_claim_lost",
                "mcp_remote_task_claim_renew_failed",
            }:
                raise
            active_entry = self._active.get(
                binding.safe_remote_task_ref, (binding, claim_token)
            )
            active_binding = active_entry[0]
            rescheduled = await self._reschedule_after_error(
                active_binding, claim_token, revision["value"]
            )
            if not rescheduled:
                raise MCPRemoteTaskRecoveryError("mcp_remote_task_claim_lost") from exc
        finally:
            await _stop_renewer(renew_stop, renewer, propagate=False)
            if client is not None:
                await _safe_close(client)
            self._active.pop(binding.safe_remote_task_ref, None)

    async def _renew_claim(
        self,
        binding: MCPRemoteTaskBinding,
        claim_token: str,
        revision: dict[str, int],
        stop: asyncio.Event,
    ) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._claim_renew_seconds)
                return
            except TimeoutError:
                pass
            now = self._now()
            try:
                renewed = await self._storage.renew_mcp_remote_task_binding_claim(
                    binding.owner_user_id,
                    binding.task_id,
                    binding.safe_remote_task_ref,
                    claim_owner=self._instance_id,
                    claim_token=claim_token,
                    expected_revision=revision["value"],
                    lease_expires_at=now + self._claim_ttl,
                    updated_at=now,
                )
            except Exception as exc:
                raise MCPRemoteTaskRecoveryError(
                    "mcp_remote_task_claim_renew_failed"
                ) from exc
            if renewed is None:
                raise MCPRemoteTaskRecoveryError("mcp_remote_task_claim_lost")
            revision["value"] = renewed.revision
            self._active[binding.safe_remote_task_ref] = (renewed, claim_token)

    async def _reschedule_after_error(
        self,
        binding: MCPRemoteTaskBinding,
        claim_token: str,
        expected_revision: int,
    ) -> bool:
        now = self._now()
        updated = await self._storage.update_mcp_remote_task_binding_status(
            binding.owner_user_id,
            binding.task_id,
            binding.safe_remote_task_ref,
            claim_owner=self._instance_id,
            claim_token=claim_token,
            expected_revision=expected_revision,
            last_status=binding.last_status,
            next_poll_at=now + self._error_backoff,
            updated_at=now,
        )
        if updated is not None:
            await self._release(updated, claim_token)
            return True
        return False

    async def _release(
        self, binding: MCPRemoteTaskBinding, claim_token: str
    ) -> MCPRemoteTaskBinding | None:
        released = await self._storage.release_mcp_remote_task_binding_claim(
            binding.owner_user_id,
            binding.task_id,
            binding.safe_remote_task_ref,
            claim_owner=self._instance_id,
            claim_token=claim_token,
            expected_revision=binding.revision,
            updated_at=self._now(),
        )
        if released is not None:
            self._active[binding.safe_remote_task_ref] = (released, claim_token)
        return released

    async def _emit(self, binding: MCPRemoteTaskBinding, status: str) -> None:
        if self._event_sink is None:
            return
        try:
            await _await_maybe(self._event_sink(binding, status))
        except Exception:
            return

    async def _record_terminal_metric(
        self,
        binding: MCPRemoteTaskBinding,
        *,
        call_status: str,
        terminal_at: datetime,
    ) -> None:
        if self._terminal_metric_sink is None:
            return
        result_category, error_category = _terminal_metric_categories(call_status)
        started_at = binding.created_at or terminal_at
        sample = MCPRemoteTaskTerminalMetricSample(
            binding=binding,
            result_category=result_category,
            error_category=error_category,
            duration_seconds=max(0.0, (terminal_at - started_at).total_seconds()),
            terminal_at=terminal_at,
        )
        try:
            await _await_maybe(self._terminal_metric_sink(sample))
        except Exception:
            await self._emit_metric_gap(binding)

    async def _emit_metric_gap(self, binding: MCPRemoteTaskBinding) -> None:
        if self._metric_gap_sink is None:
            raise MCPRemoteTaskRecoveryError(
                "mcp_terminal_metric_gap_sink_missing"
            )
        await _await_maybe(
            self._metric_gap_sink(binding, "terminal_recording_failed")
        )

    async def _record_active_metric(self) -> None:
        if self._active_metric_sink is None:
            return
        try:
            await _await_maybe(self._active_metric_sink())
        except Exception:
            if self._global_metric_gap_sink is None:
                return
            await _await_maybe(
                self._global_metric_gap_sink("active_gauge_recording_failed")
            )

    async def _record_global_metric_gap(self, reason: str) -> None:
        if self._global_metric_gap_sink is None:
            return
        try:
            await _await_maybe(self._global_metric_gap_sink(reason))
        except Exception:
            return


async def _safe_close(value: Any) -> None:
    close = getattr(value, "aclose", None) or getattr(value, "close", None)
    if callable(close):
        try:
            await _await_maybe(close())
        except Exception:
            return


async def _stop_renewer(
    stop: asyncio.Event,
    renewer: asyncio.Task[None],
    *,
    propagate: bool = True,
) -> None:
    stop.set()
    outcomes = await asyncio.gather(renewer, return_exceptions=True)
    if propagate and outcomes and isinstance(outcomes[0], BaseException):
        raise outcomes[0]


async def _await_maybe(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _terminal_statuses(status: str) -> tuple[str, str, str | None]:
    if status == "completed":
        return "completed", "completed", None
    if status == "failed":
        return "failed", "failed", "mcp_remote_task_failed"
    if status in {"cancelled", "canceled"}:
        return "cancelled", "cancelled", "mcp_remote_task_cancelled"
    return "unknown", "unknown", "execution_status_unknown"


def _terminal_metric_categories(
    call_status: str,
) -> tuple[MCPMetricResultCategory, MCPMetricErrorCategory]:
    if call_status == "completed":
        return MCPMetricResultCategory.SUCCEEDED, MCPMetricErrorCategory.NONE
    if call_status == "failed":
        return MCPMetricResultCategory.FAILED, MCPMetricErrorCategory.SERVER
    if call_status == "cancelled":
        return MCPMetricResultCategory.CANCELLED, MCPMetricErrorCategory.NONE
    return MCPMetricResultCategory.UNKNOWN, MCPMetricErrorCategory.UNKNOWN


__all__ = [
    "MCPContinuationAdmissionResult",
    "MCP2025RemoteTaskProtocolHandler",
    "MCP2026RemoteTaskProtocolHandler",
    "MCPRemoteTaskPollResult",
    "MCPRemoteTaskProtocolHandler",
    "MCPRemoteTaskRecoveryError",
    "MCPRemoteTaskRecoveryWorker",
    "MCPRemoteTaskTerminalMetricSample",
    "RemoteTaskMetricGapSink",
    "RemoteTaskActiveMetricSink",
    "RemoteTaskGlobalMetricGapSink",
    "RemoteTaskResultPersister",
    "RemoteTaskContinuationSink",
    "RemoteTaskTerminalMetricSink",
]
