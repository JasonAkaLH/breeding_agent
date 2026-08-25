from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.core.contracts import AuditSink, EventSink, MailboxStoragePort
from src.core.models import MailboxDelivery, MailboxMessage

from . import task_state_machine


class MailboxService:
    def __init__(
        self,
        storage: MailboxStoragePort,
        *,
        event_sink: EventSink | None = None,
        audit_sink: AuditSink | None = None,
        retry_delay: timedelta = timedelta(seconds=30),
    ) -> None:
        self._storage = storage
        self._event_sink = event_sink
        self._audit_sink = audit_sink
        self._retry_delay = retry_delay

    @staticmethod
    def _utcnow_naive() -> datetime:
        return datetime.now(timezone.utc).replace(tzinfo=None)

    async def _get_message_and_delivery(self, delivery_id: str) -> tuple[MailboxMessage, MailboxDelivery]:
        delivery = await self._storage.get_mailbox_delivery(delivery_id)
        if delivery is None:
            raise ValueError(f"Unknown mailbox delivery: {delivery_id}")
        message = await self._storage.get_mailbox_message(delivery.message_id)
        if message is None:
            raise ValueError(f"Unknown mailbox message: {delivery.message_id}")
        return message, delivery

    async def mark_delivered(self, delivery_id: str, *, now: datetime | None = None) -> MailboxDelivery:
        _, delivery = await self._get_message_and_delivery(delivery_id)
        updated = task_state_machine.mark_delivery_delivered(delivery, now=now or self._utcnow_naive())
        return await self._storage.save_mailbox_delivery(updated)

    async def acknowledge_delivery(self, delivery_id: str, *, now: datetime | None = None) -> MailboxDelivery:
        message, delivery = await self._get_message_and_delivery(delivery_id)
        updated = task_state_machine.acknowledge_delivery(message, delivery, now=now or self._utcnow_naive())
        return await self._storage.save_mailbox_delivery(updated)

    async def resolve_delivery(self, delivery_id: str, *, now: datetime | None = None) -> MailboxDelivery:
        message, delivery = await self._get_message_and_delivery(delivery_id)
        updated = task_state_machine.resolve_delivery(message, delivery, now=now or self._utcnow_naive())
        return await self._storage.save_mailbox_delivery(updated)

    async def handle_delivery_timeout(self, delivery_id: str, *, now: datetime | None = None) -> MailboxDelivery:
        message, delivery = await self._get_message_and_delivery(delivery_id)
        current_time = now or self._utcnow_naive()
        updated = task_state_machine.handle_delivery_timeout(delivery, now=current_time, retry_delay=self._retry_delay)
        saved = await self._storage.save_mailbox_delivery(updated)
        if self._audit_sink is not None and saved.status != delivery.status:
            await self._audit_sink.record(
                "mailbox.delivery_timeout_handled",
                {
                    "message_id": message.message_id,
                    "delivery_id": saved.delivery_id,
                    "status": str(saved.status),
                    "attempt_count": saved.attempt_count,
                },
                conversation_id=message.conversation_id,
                task_id=message.task_id,
                node_id=message.node_id,
            )
        return saved
