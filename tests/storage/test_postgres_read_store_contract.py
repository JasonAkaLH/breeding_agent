from __future__ import annotations

import unittest
from datetime import datetime, timezone

from src.core.enums import ConversationStatus, MessageRole
from src.core.models import Conversation, Message
from src.state.postgres.read_store import READ_SQL_BY_OPERATION, InMemoryPostgresReadStore


class PostgresReadStoreContractTest(unittest.IsolatedAsyncioTestCase):
    def test_read_sql_never_reads_pending_queue_or_uses_write_locks(self) -> None:
        for sql in READ_SQL_BY_OPERATION.values():
            normalized = sql.upper()
            self.assertNotIn("STATE_WRITE_COMMAND", normalized)
            self.assertNotIn("FOR UPDATE", normalized)
            self.assertNotIn("FOR SHARE", normalized)

    async def test_pending_queue_is_invisible_to_read_store(self) -> None:
        store = InMemoryPostgresReadStore(
            conversations={"c1": Conversation("c1", "alice", status=ConversationStatus.ACTIVE)},
            messages={"c1": [Message("m1", "c1", MessageRole.USER, "v1", created_at=datetime.now(timezone.utc).replace(tzinfo=None))]},
        )
        conversation = await store.get_conversation("c1")
        self.assertIsNotNone(conversation)
        self.assertEqual(conversation.conversation_id, "c1")  # type: ignore[union-attr]
        self.assertEqual([m.content for m in await store.list_messages_for_conversation("c1")], ["v1"])
