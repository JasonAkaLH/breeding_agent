from __future__ import annotations

import asyncio
from datetime import datetime

from src.core.enums import AckPolicy, MailboxChannel
from src.core.models import MailboxDelivery, MailboxMessage
from src.lifecycle.errors import LifecycleTransitionError
from src.lifecycle.mailbox_service import MailboxService
from tests.lifecycle.support import LifecycleSQLiteTestCase


class MailboxAckFlowTest(LifecycleSQLiteTestCase):
    def test_strong_ack_requires_acknowledge_before_resolve(self) -> None:
        message = MailboxMessage(
            message_id="mail-strong-1",
            conversation_id="conv-1",
            task_id="task-1",
            channel=MailboxChannel.ORCHESTRATOR_CONTROL,
            message_type="node_assignment",
            ack_policy=AckPolicy.STRONG,
            created_at=datetime(2026, 4, 23, 14, 0, 0),
        )
        delivery = MailboxDelivery(
            delivery_id="delivery-strong-1",
            message_id="mail-strong-1",
            recipient_agent="worker-1",
            ttl_seconds=60,
            expires_at=datetime(2026, 4, 23, 14, 1, 0),
            created_at=datetime(2026, 4, 23, 14, 0, 0),
            updated_at=datetime(2026, 4, 23, 14, 0, 0),
        )
        service = MailboxService(self.storage)

        asyncio.run(self.storage.save_mailbox_message(message))
        asyncio.run(self.storage.save_mailbox_delivery(delivery))
        asyncio.run(service.mark_delivered("delivery-strong-1", now=datetime(2026, 4, 23, 14, 0, 1)))

        with self.assertRaises(LifecycleTransitionError):
            asyncio.run(service.resolve_delivery("delivery-strong-1", now=datetime(2026, 4, 23, 14, 0, 2)))

        asyncio.run(service.acknowledge_delivery("delivery-strong-1", now=datetime(2026, 4, 23, 14, 0, 3)))
        resolved = asyncio.run(service.resolve_delivery("delivery-strong-1", now=datetime(2026, 4, 23, 14, 0, 4)))

        self.assertEqual(resolved.status, "resolved")
        self.assertIsNotNone(resolved.acknowledged_at)
        self.assertIsNotNone(resolved.resolved_at)

    def test_light_ack_can_resolve_after_delivery_without_acknowledge(self) -> None:
        message = MailboxMessage(
            message_id="mail-light-1",
            conversation_id="conv-1",
            task_id="task-1",
            channel=MailboxChannel.PEER_COLLABORATION,
            message_type="dependency_response",
            ack_policy=AckPolicy.LIGHT,
            created_at=datetime(2026, 4, 23, 14, 10, 0),
        )
        delivery = MailboxDelivery(
            delivery_id="delivery-light-1",
            message_id="mail-light-1",
            recipient_agent="worker-2",
            ttl_seconds=60,
            expires_at=datetime(2026, 4, 23, 14, 11, 0),
            created_at=datetime(2026, 4, 23, 14, 10, 0),
            updated_at=datetime(2026, 4, 23, 14, 10, 0),
        )
        service = MailboxService(self.storage)

        asyncio.run(self.storage.save_mailbox_message(message))
        asyncio.run(self.storage.save_mailbox_delivery(delivery))
        asyncio.run(service.mark_delivered("delivery-light-1", now=datetime(2026, 4, 23, 14, 10, 1)))
        resolved = asyncio.run(service.resolve_delivery("delivery-light-1", now=datetime(2026, 4, 23, 14, 10, 2)))

        self.assertEqual(resolved.status, "resolved")
        self.assertIsNone(resolved.acknowledged_at)
