from __future__ import annotations

from datetime import datetime

from src.core.enums import ConversationStatus, MessageRole
from src.core.models import Conversation, Message
from src.storage.sqlite.repositories import SQLiteStateRepository
from tests.storage.support import SQLiteStorageTestCase


class SQLiteConversationRepositoryTest(SQLiteStorageTestCase):
    def test_conversation_round_trip(self) -> None:
        conversation = Conversation(
            conversation_id="conv-1",
            username="acc-1",
            status=ConversationStatus.ACTIVE,
            current_task_id="task-1",
            title="conversation title",
            created_at=datetime(2026, 4, 23, 10, 0, 0),
            updated_at=datetime(2026, 4, 23, 10, 5, 0),
        )

        with self.session_factory() as session:
            repo = SQLiteStateRepository(session)
            saved = repo.save_conversation(conversation)
            session.commit()

        with self.session_factory() as session:
            repo = SQLiteStateRepository(session)
            loaded = repo.get_conversation("conv-1")

        self.assertEqual(saved, conversation)
        self.assertEqual(loaded, conversation)

    def test_message_round_trip_and_conversation_listing(self) -> None:
        conversation = Conversation(conversation_id="conv-1", username="acc-1")
        message = Message(
            message_id="msg-1",
            conversation_id="conv-1",
            role=MessageRole.USER,
            content="hello",
            task_id="task-1",
            created_at=datetime(2026, 4, 23, 11, 0, 0),
        )

        with self.session_factory() as session:
            repo = SQLiteStateRepository(session)
            repo.save_conversation(conversation)
            saved = repo.save_message(message)
            session.commit()

        with self.session_factory() as session:
            repo = SQLiteStateRepository(session)
            loaded = repo.get_message("msg-1")
            listed = repo.list_messages_for_conversation("conv-1")

        self.assertEqual(saved, message)
        self.assertEqual(loaded, message)
        self.assertEqual(listed, [message])
