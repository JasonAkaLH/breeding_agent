from __future__ import annotations

from datetime import datetime

from src.core.enums import AckPolicy, EventVisibility, MailboxChannel, MailboxDeliveryStatus
from src.core.models import EventRecord, MailboxDelivery, MailboxMessage
from src.storage.sqlite.repositories import SQLiteCollaborationRepository
from tests.storage.support import SQLiteStorageTestCase


class SQLiteMailboxRepositoryTest(SQLiteStorageTestCase):
    def test_event_record_round_trip(self) -> None:
        event = EventRecord(
            event_id="evt-1",
            conversation_id="conv-1",
            task_id="task-1",
            node_id="node-1",
            agent_id="agent-1",
            event_type="task.accepted",
            payload={"status": "accepted"},
            visibility=EventVisibility.FRONTEND,
            created_at=datetime(2026, 4, 23, 12, 0, 0),
        )

        with self.session_factory() as session:
            repo = SQLiteCollaborationRepository(session)
            saved = repo.save_event_record(event)
            session.commit()

        with self.session_factory() as session:
            repo = SQLiteCollaborationRepository(session)
            loaded = repo.get_event_record("evt-1")
            listed = repo.list_events_for_task("task-1")

        self.assertEqual(saved, event)
        self.assertEqual(loaded, event)
        self.assertEqual(listed, [event])

    def test_mailbox_message_and_delivery_round_trip(self) -> None:
        mailbox = MailboxMessage(
            message_id="mail-1",
            conversation_id="conv-1",
            task_id="task-1",
            node_id="node-1",
            correlation_id="corr-1",
            from_agent="orchestrator",
            to_agent="worker-1",
            to_role="worker",
            channel=MailboxChannel.ORCHESTRATOR_CONTROL,
            message_type="dispatch",
            ack_policy=AckPolicy.STRONG,
            priority=10,
            payload={"node_id": "node-1"},
            payload_schema_version=2,
            created_at=datetime(2026, 4, 23, 12, 1, 0),
        )
        delivery = MailboxDelivery(
            delivery_id="delivery-1",
            message_id="mail-1",
            recipient_agent="worker-1",
            recipient_role="worker",
            status=MailboxDeliveryStatus.DELIVERED,
            attempt_count=1,
            max_attempts=3,
            ttl_seconds=60,
            expires_at=datetime(2026, 4, 23, 12, 2, 0),
            delivered_at=datetime(2026, 4, 23, 12, 1, 1),
            created_at=datetime(2026, 4, 23, 12, 1, 0),
            updated_at=datetime(2026, 4, 23, 12, 1, 1),
        )

        with self.session_factory() as session:
            repo = SQLiteCollaborationRepository(session)
            repo.save_mailbox_message(mailbox)
            repo.save_mailbox_delivery(delivery)
            session.commit()

        with self.session_factory() as session:
            repo = SQLiteCollaborationRepository(session)
            loaded_mailbox = repo.get_mailbox_message("mail-1")
            loaded_delivery = repo.get_mailbox_delivery("delivery-1")
            listed_deliveries = repo.list_mailbox_deliveries_for_message("mail-1")

        self.assertEqual(loaded_mailbox, mailbox)
        self.assertEqual(loaded_delivery, delivery)
        self.assertEqual(listed_deliveries, [delivery])

    def test_duplicate_mailbox_delivery_for_same_recipient_updates_single_row(self) -> None:
        first = MailboxDelivery(
            delivery_id="delivery-1",
            message_id="mail-1",
            recipient_agent="worker-1",
            status=MailboxDeliveryStatus.PENDING,
            attempt_count=0,
            max_attempts=3,
            ttl_seconds=60,
            expires_at=datetime(2026, 4, 23, 12, 2, 0),
            created_at=datetime(2026, 4, 23, 12, 1, 0),
            updated_at=datetime(2026, 4, 23, 12, 1, 0),
        )
        second = MailboxDelivery(
            delivery_id="delivery-2",
            message_id="mail-1",
            recipient_agent="worker-1",
            status=MailboxDeliveryStatus.ACKNOWLEDGED,
            attempt_count=1,
            max_attempts=3,
            ttl_seconds=60,
            expires_at=datetime(2026, 4, 23, 12, 3, 0),
            acknowledged_at=datetime(2026, 4, 23, 12, 1, 30),
            created_at=datetime(2026, 4, 23, 12, 1, 0),
            updated_at=datetime(2026, 4, 23, 12, 1, 30),
        )

        with self.session_factory() as session:
            repo = SQLiteCollaborationRepository(session)
            repo.save_mailbox_delivery(first)
            repo.save_mailbox_delivery(second)
            session.commit()

        with self.session_factory() as session:
            repo = SQLiteCollaborationRepository(session)
            deliveries = repo.list_mailbox_deliveries_for_message("mail-1")

        self.assertEqual(len(deliveries), 1)
        self.assertEqual(deliveries[0].status, MailboxDeliveryStatus.ACKNOWLEDGED)
        self.assertEqual(deliveries[0].attempt_count, 1)
