from __future__ import annotations

import inspect
from datetime import datetime, timezone
from typing import Any

from src.core.contracts import AuditSink, EventSink, StoragePort
from src.core.enums import EventVisibility
from src.core.models import EventRecord
from src.storage.rust_contract import mode_for_component
from src.storage.runtime_sidecar_facade import ensure_sidecar_write_allowed, validate_runtime_sidecar_response

from . import task_state_machine


class CancellationService:
    def __init__(
        self,
        storage: StoragePort,
        *,
        event_sink: EventSink | None = None,
        audit_sink: AuditSink | None = None,
        runtime_sidecar_client: Any | None = None,
    ) -> None:
        self._storage = storage
        self._event_sink = event_sink
        self._audit_sink = audit_sink
        self._runtime_sidecar_client = runtime_sidecar_client

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
        return EventRecord(
            event_id=f"evt-{task_id}-{event_type}-{suffix}",
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

        await self._write_cancellation_token(task_id=task_id, requested_at=current_time)
        cancelling_task = task_state_machine.begin_task_cancellation(task, now=current_time)
        await self._storage.save_task(cancelling_task)
        await self._record_event(
            self._make_event(
                task_id=task.task_id,
                conversation_id=task.conversation_id,
                event_type="task.cancellation_requested",
                payload={"status": str(cancelling_task.status)},
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

        cancelled_task = task_state_machine.finalize_task_cancellation(cancelling_task, now=current_time)
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


def _ensure_cancellation_token_write_allowed_by_rust_contract() -> None:
    ensure_sidecar_write_allowed(
        component="runtime_store",
        operation_name="cancellation_token_write",
        unavailable_error_code="runtime_store_unavailable",
    )
