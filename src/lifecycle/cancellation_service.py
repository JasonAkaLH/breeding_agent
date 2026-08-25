from __future__ import annotations

import inspect
from datetime import datetime, timezone
from typing import Any, Awaitable, Mapping, Protocol

from src.core.contracts import (
    AuditSink,
    CheckpointStoragePort,
    EventSink,
    EventStoragePort,
    InterruptStoragePort,
    MailboxStoragePort,
    TaskStoragePort,
)
from src.core.enums import EventVisibility
from src.core.models import EventRecord
from src.orchestration.agent_loop.models import AgentRun, AgentRunStatus
from src.storage.rust_contract import mode_for_component
from src.storage.runtime_sidecar_facade import ensure_sidecar_write_allowed, validate_runtime_sidecar_response
from src.storage.runtime_sidecar_shadow import record_runtime_sidecar_shadow_write

from . import task_state_machine


class CancellationSidecarWriter(Protocol):
    def write_cancellation_token(
        self,
        *,
        task_id: str,
        requested_at_ms: int,
        reason: str,
        terminal_policy: str,
        idempotency_key: str,
    ) -> Mapping[str, Any] | Awaitable[Mapping[str, Any]]: ...


class AgentCancellationStore(Protocol):
    async def get_run_for_task(self, task_id: str) -> AgentRun | None: ...

    async def cancel_agent_run(
        self,
        run_id: str,
        *,
        expected_revision: int,
        expected_claim_token: str | None,
        safe_reason_code: str,
    ) -> AgentRun: ...


class CancellationLifecycleStoragePort(
    TaskStoragePort,
    InterruptStoragePort,
    MailboxStoragePort,
    CheckpointStoragePort,
    EventStoragePort,
    Protocol,
):
    pass


class CancellationService:
    def __init__(
        self,
        storage: CancellationLifecycleStoragePort,
        *,
        event_sink: EventSink | None = None,
        audit_sink: AuditSink | None = None,
        runtime_sidecar_client: CancellationSidecarWriter | None = None,
        agent_runs: AgentCancellationStore | None = None,
    ) -> None:
        self._storage = storage
        self._event_sink = event_sink
        self._audit_sink = audit_sink
        self._runtime_sidecar_client = runtime_sidecar_client
        self._agent_runs = agent_runs

    @staticmethod
    def _utcnow_naive() -> datetime:
        return datetime.now(timezone.utc).replace(tzinfo=None)

    async def _record_event(self, event: EventRecord) -> None:
        await self._storage.append_event(event)
        if self._event_sink is not None:
            await self._event_sink.publish(event)

    def _make_event(
        self,
        *,
        task_id: str,
        conversation_id: str,
        event_type: str,
        node_id: str | None = None,
        payload: dict | None = None,
        visibility: EventVisibility = EventVisibility.FRONTEND,
        now: datetime | None = None,
    ) -> EventRecord:
        suffix = int((now or self._utcnow_naive()).timestamp() * 1_000_000)
        node_part = node_id or "task"
        return EventRecord(
            event_id=f"evt-{task_id}-{event_type}-{node_part}-{suffix}",
            conversation_id=conversation_id,
            task_id=task_id,
            node_id=node_id,
            event_type=event_type,
            payload=payload or {},
            visibility=visibility,
            created_at=now or self._utcnow_naive(),
        )

    async def cancel_task_context(self, task_id: str, *, now: datetime | None = None):
        current_time = now or self._utcnow_naive()
        task = await self._storage.get_task(task_id)
        if task is None:
            raise ValueError(f"Unknown task: {task_id}")
        if task_state_machine.is_task_cancellation_noop(task):
            return task

        if self._agent_runs is not None:
            run = await self._agent_runs.get_run_for_task(task_id)
            if run is not None and run.status not in {
                AgentRunStatus.COMPLETED,
                AgentRunStatus.FAILED,
                AgentRunStatus.CANCELLED,
            }:
                await self._write_cancellation_token(
                    task_id=task_id,
                    requested_at=current_time,
                )
                cancelled_run = await self._agent_runs.cancel_agent_run(
                    run.run_id,
                    expected_revision=run.revision,
                    expected_claim_token=run.claim_token,
                    safe_reason_code="user_cancel",
                )
                saved_task = await self._storage.get_task(task_id)
                if saved_task is None:
                    raise RuntimeError("agent_cancel_task_projection_missing")
                await self._record_event(
                    self._make_event(
                        task_id=saved_task.task_id,
                        conversation_id=saved_task.conversation_id,
                        event_type="task.cancelled",
                        payload={
                            "status": str(saved_task.status),
                            "agent_run_status": cancelled_run.status.value,
                        },
                        now=current_time,
                    )
                )
                return saved_task

        await self._write_cancellation_token(task_id=task_id, requested_at=current_time)
        cancelling_task = task_state_machine.begin_task_cancellation(task, now=current_time)
        saved_cancelling_task = await self._storage.save_task(cancelling_task)
        await self._record_cancellation_token_shadow(
            task_id=task_id,
            requested_at=current_time,
            legacy_output={
                "cancel_requested_at_ms": str(int(current_time.timestamp() * 1000)),
                "status": str(saved_cancelling_task.status),
                "task_id": saved_cancelling_task.task_id,
            },
        )
        await self._record_event(
            self._make_event(
                task_id=task.task_id,
                conversation_id=task.conversation_id,
                event_type="task.cancellation_requested",
                payload={"status": str(saved_cancelling_task.status)},
                now=current_time,
            )
        )

        nodes = await self._storage.list_task_nodes_for_task(task_id)
        for node in nodes:
            updated = task_state_machine.cancel_node(node)
            if updated != node:
                await self._storage.save_task_node(updated)
                await self._record_event(
                    self._make_event(
                        task_id=task.task_id,
                        conversation_id=task.conversation_id,
                        node_id=node.node_id,
                        event_type="node.cancelled" if str(updated.status) == "cancelled" else "node.blocked_by_cancellation",
                        payload={"status": str(updated.status), "capability_id": node.capability_id},
                        now=current_time,
                    )
                )

        interrupts = await self._storage.list_interrupts_for_task(task_id)
        for interrupt in interrupts:
            updated = task_state_machine.cancel_interrupt(interrupt, now=current_time)
            if updated != interrupt:
                await self._storage.save_interrupt(updated)

        checkpoints = await self._storage.list_checkpoints_for_task(task_id)
        for checkpoint in checkpoints:
            updated = task_state_machine.invalidate_checkpoint(checkpoint, now=current_time)
            if updated != checkpoint:
                await self._storage.save_checkpoint(updated)

        messages = await self._storage.list_mailbox_messages_for_task(task_id)
        for message in messages:
            deliveries = await self._storage.list_mailbox_deliveries_for_message(message.message_id)
            for delivery in deliveries:
                updated_delivery = task_state_machine.cancel_mailbox_delivery(delivery, now=current_time)
                if updated_delivery != delivery:
                    await self._storage.save_mailbox_delivery(updated_delivery)

        cancelled_task = task_state_machine.finalize_task_cancellation(saved_cancelling_task, now=current_time)
        saved_task = await self._storage.save_task(cancelled_task)
        await self._record_event(
            self._make_event(
                task_id=saved_task.task_id,
                conversation_id=saved_task.conversation_id,
                event_type="task.cancelled",
                payload={"status": str(saved_task.status)},
                now=current_time,
            )
        )
        if self._audit_sink is not None:
            await self._audit_sink.record(
                "task.context_terminated",
                {"task_id": task_id, "status": str(saved_task.status)},
                conversation_id=saved_task.conversation_id,
                task_id=saved_task.task_id,
            )
        return saved_task

    async def can_accept_late_result(self, task_id: str) -> bool:
        task = await self._storage.get_task(task_id)
        return task_state_machine.can_accept_late_result(task)

    async def _write_cancellation_token(self, *, task_id: str, requested_at: datetime) -> None:
        if mode_for_component("runtime_store") != "enforce":
            _ensure_cancellation_token_write_allowed_by_rust_contract()
            return
        if self._runtime_sidecar_client is None:
            _ensure_cancellation_token_write_allowed_by_rust_contract()
            return
        response = self._runtime_sidecar_client.write_cancellation_token(
            task_id=task_id,
            requested_at_ms=int(requested_at.timestamp() * 1000),
            reason="user_cancel",
            terminal_policy="terminal-noop",
            idempotency_key=f"{task_id}:cancellation_token",
        )
        response = await response if inspect.isawaitable(response) else response
        envelope = validate_runtime_sidecar_response("cancellation_token_write", response)
        error = envelope.get("error")
        if isinstance(error, dict):
            raise RuntimeError(f"{error['code']}: {error['message']}")

    async def _record_cancellation_token_shadow(
        self,
        *,
        task_id: str,
        requested_at: datetime,
        legacy_output: dict[str, Any],
    ) -> None:
        requested_at_ms = int(requested_at.timestamp() * 1000)

        async def record_shadow_diff(payload: dict[str, str]) -> None:
            if self._audit_sink is None:
                return
            await self._audit_sink.record("runtime.sidecar_shadow_diff", payload, task_id=task_id)

        await record_runtime_sidecar_shadow_write(
            component="runtime_store",
            operation_name="cancellation_token_write",
            runtime_sidecar_client=self._runtime_sidecar_client,
            shadow_sink=record_shadow_diff if self._audit_sink is not None else None,
            input_payload={
                "reason": "user_cancel",
                "requested_at_ms": requested_at_ms,
                "task_id": task_id,
                "terminal_policy": "terminal-noop",
            },
            legacy_output=legacy_output,
            rust_call=lambda: self._runtime_sidecar_client.write_cancellation_token(
                task_id=task_id,
                requested_at_ms=requested_at_ms,
                reason="user_cancel",
                terminal_policy="terminal-noop",
                idempotency_key=f"{task_id}:cancellation_token",
            ),
            rust_output=lambda envelope: {
                "task_id": str(envelope.get("task_id", "")),
                "written": str(envelope.get("written", "")),
            },
        )


def _ensure_cancellation_token_write_allowed_by_rust_contract() -> None:
    ensure_sidecar_write_allowed(
        component="runtime_store",
        operation_name="cancellation_token_write",
        unavailable_error_code="runtime_store_unavailable",
    )
