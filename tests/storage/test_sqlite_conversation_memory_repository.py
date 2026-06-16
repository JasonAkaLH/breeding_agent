from __future__ import annotations

import asyncio
from datetime import datetime

from sqlalchemy import func, select

from src.core.models import Conversation, ConversationMemorySummary
from src.storage.sqlite.models import ConversationMemorySummaryRow
from src.storage.sqlite.repositories import SQLiteStateRepository, SQLiteStorage
from tests.storage.support import SQLiteStorageTestCase


class SQLiteConversationMemoryRepositoryTest(SQLiteStorageTestCase):
    def test_memory_summary_round_trip_and_latest_scope(self) -> None:
        now = datetime(2026, 5, 8, 10, 0, 0)
        older = ConversationMemorySummary(
            summary_id="summary-old",
            conversation_id="conv-1",
            username="alice",
            covered_until_turn_id="task-1",
            covered_until_message_id="msg-1-assistant",
            covered_until_created_at=now,
            summary_text="较早摘要",
            source_message_count=2,
            source_message_ids_hash="hash-old",
            estimated_tokens=12,
            summary_version="v1",
            compression_policy_version="policy-v1",
            model_metadata_safe={"provider": "fake", "model": "test"},
            created_at=now,
            updated_at=now,
        )
        newer = ConversationMemorySummary(
            summary_id="summary-new",
            conversation_id="conv-1",
            username="alice",
            covered_until_turn_id="task-2",
            covered_until_message_id="msg-2-assistant",
            covered_until_created_at=now,
            summary_text="最新摘要",
            source_message_count=4,
            source_message_ids_hash="hash-new",
            estimated_tokens=20,
            summary_version="v1",
            compression_policy_version="policy-v1",
            model_metadata_safe={"provider": "fake", "duration_ms": 3},
            created_at=now,
            updated_at=now,
        )
        bob = ConversationMemorySummary(
            summary_id="summary-bob",
            conversation_id="conv-2",
            username="bob",
            covered_until_turn_id="task-bob",
            covered_until_message_id="msg-bob",
            covered_until_created_at=now,
            summary_text="Bob 摘要",
            source_message_count=1,
            source_message_ids_hash="hash-bob",
            estimated_tokens=5,
            summary_version="v1",
            compression_policy_version="policy-v1",
            created_at=now,
            updated_at=now,
        )

        with self.session_factory() as session:
            repo = SQLiteStateRepository(session)
            repo.save_conversation(Conversation(conversation_id="conv-1", username="alice"))
            repo.save_conversation(Conversation(conversation_id="conv-2", username="bob"))
            repo.save_conversation_memory_summary(older)
            saved = repo.save_conversation_memory_summary(newer)
            repo.save_conversation_memory_summary(bob)
            session.commit()

        with self.session_factory() as session:
            repo = SQLiteStateRepository(session)
            loaded = repo.get_conversation_memory_summary("summary-new")
            latest = repo.get_latest_conversation_memory_summary("conv-1", username="alice")
            bob_blocked = repo.get_latest_conversation_memory_summary("conv-1", username="bob")
            listed = repo.list_conversation_memory_summaries("conv-1")

        self.assertEqual(saved, newer)
        self.assertEqual(loaded, newer)
        self.assertEqual(latest, newer)
        self.assertIsNone(bob_blocked)
        self.assertEqual([item.summary_id for item in listed], ["summary-new", "summary-old"])
        self.assertNotIn("api_key", str(latest.model_metadata_safe))
        self.assertNotIn("base_url", str(latest.model_metadata_safe))

    def test_delete_conversation_purges_memory_summaries(self) -> None:
        now = datetime(2026, 5, 8, 10, 0, 0)
        with self.session_factory() as session:
            repo = SQLiteStateRepository(session)
            repo.save_conversation(Conversation(conversation_id="conv-delete-memory", username="alice"))
            repo.save_conversation_memory_summary(
                ConversationMemorySummary(
                    summary_id="summary-delete",
                    conversation_id="conv-delete-memory",
                    username="alice",
                    covered_until_turn_id="task-1",
                    covered_until_message_id="msg-1",
                    covered_until_created_at=now,
                    summary_text="待删除摘要",
                    source_message_count=1,
                    source_message_ids_hash="hash",
                    estimated_tokens=4,
                    summary_version="v1",
                    compression_policy_version="policy-v1",
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()

        storage = SQLiteStorage(self.session_factory)
        deleted_counts = asyncio.run(storage.delete_conversation("conv-delete-memory"))

        self.assertGreaterEqual(deleted_counts["conversation_memory_summary"], 1)
        with self.session_factory() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(ConversationMemorySummaryRow)), 0)
