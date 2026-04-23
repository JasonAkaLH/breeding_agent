from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from src.core.contracts import AuditSink, EventSink, StoragePort
from src.core.enums import EventVisibility, MailboxDeliveryStatus
from src.core.models import EventRecord, MailboxDelivery

from . import task_state_machine


class CancellationService:
    def __init__(
        self,
        storage: StoragePort,
        *,
        event_sink: EventSink | None = None,
        audit_sink: AuditSink | None = None,
    ) -> None:
        self._storage = storage
        self._event_sink = event_sink
        self._audit_sink = audit_sink

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
                if delivery.status in {
                    MailboxDeliveryStatus.RESOLVED,
                    MailboxDeliveryStatus.CANCELLED,
                    MailboxDeliveryStatus.EXPIRED,
                }:
                    continue
                updated_delivery = replace(
                    delivery,
                    status=MailboxDeliveryStatus.CANCELLED,
                    resolved_at=delivery.resolved_at or current_time,
                    updated_at=current_time,
                )
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
