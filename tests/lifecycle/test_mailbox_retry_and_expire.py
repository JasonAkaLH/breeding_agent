from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from src.core.enums import AckPolicy, MailboxChannel, MailboxDeliveryStatus
from src.core.models import MailboxDelivery, MailboxMessage
from src.lifecycle.mailbox_service import MailboxService
from tests.lifecycle.support import LifecycleSQLiteTestCase


class MailboxRetryAndExpireTest(LifecycleSQLiteTestCase):
    def test_timeout_requeues_delivery_when_attempts_remain(self) -> None:
        message = MailboxMessage(
            message_id="mail-retry-1",
            conversation_id="conv-1",
            task_id="task-1",
            channel=MailboxChannel.ORCHESTRATOR_CONTROL,
            message_type="cancel_notice",
            ack_policy=AckPolicy.STRONG,
            created_at=datetime(2026, 4, 23, 15, 0, 0),
        )
        delivery = MailboxDelivery(
            delivery_id="delivery-retry-1",
            message_id="mail-retry-1",
            recipient_agent="worker-1",
            status=MailboxDeliveryStatus.DELIVERED,
            attempt_count=0,
            max_attempts=2,
            ttl_seconds=60,
            expires_at=datetime(2026, 4, 23, 15, 1, 0),
            created_at=datetime(2026, 4, 23, 15, 0, 0),
            updated_at=datetime(2026, 4, 23, 15, 0, 0),
        )
        service = MailboxService(self.storage, retry_delay=timedelta(seconds=30))

        asyncio.run(self.storage.save_mailbox_message(message))
        asyncio.run(self.storage.save_mailbox_delivery(delivery))
        updated = asyncio.run(service.handle_delivery_timeout("delivery-retry-1", now=datetime(2026, 4, 23, 15, 1, 1)))

        self.assertEqual(updated.status, MailboxDeliveryStatus.PENDING)
        self.assertEqual(updated.attempt_count, 1)
        self.assertEqual(updated.next_retry_at, datetime(2026, 4, 23, 15, 1, 31))
        self.assertEqual(updated.last_error_code, "ttl_expired")

    def test_timeout_expires_delivery_when_attempts_exhausted(self) -> None:
        message = MailboxMessage(
            message_id="mail-expire-1",
            conversation_id="conv-1",
            task_id="task-1",
            channel=MailboxChannel.ORCHESTRATOR_CONTROL,
            message_type="resume_notice",
            ack_policy=AckPolicy.STRONG,
            created_at=datetime(2026, 4, 23, 15, 10, 0),
        )
        delivery = MailboxDelivery(
            delivery_id="delivery-expire-1",
            message_id="mail-expire-1",
            recipient_agent="worker-1",
            status=MailboxDeliveryStatus.DELIVERED,
            attempt_count=1,
            max_attempts=2,
            ttl_seconds=60,
            expires_at=datetime(2026, 4, 23, 15, 11, 0),
            created_at=datetime(2026, 4, 23, 15, 10, 0),
            updated_at=datetime(2026, 4, 23, 15, 10, 0),
        )
        service = MailboxService(self.storage)

        asyncio.run(self.storage.save_mailbox_message(message))
        asyncio.run(self.storage.save_mailbox_delivery(delivery))
        updated = asyncio.run(service.handle_delivery_timeout("delivery-expire-1", now=datetime(2026, 4, 23, 15, 11, 1)))

        self.assertEqual(updated.status, MailboxDeliveryStatus.EXPIRED)
        self.assertEqual(updated.attempt_count, 2)
        self.assertIsNone(updated.next_retry_at)
