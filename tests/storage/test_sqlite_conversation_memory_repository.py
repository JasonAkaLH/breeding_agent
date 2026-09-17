from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
from unittest.mock import patch

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.core.enums import ConversationStatus
from src.core.models import Conversation, ConversationMemorySummary
from src.storage.sqlite.models import ConversationMemorySummaryRow, ConversationRow
from src.storage.sqlite.repositories import SQLiteStateRepository, SQLiteStorage
from tests.storage.support import SQLiteStorageTestCase


class SQLiteConversationMemoryRepositoryTest(SQLiteStorageTestCase):
    def test_exact_summary_materialization_replays_and_rejects_every_field_drift(self) -> None:
        storage = SQLiteStorage(self.session_factory)
        summary = _exact_summary()
        asyncio.run(
            storage.save_conversation(
                Conversation(conversation_id="conv-1", username="alice")
            )
        )

        first = asyncio.run(
            storage.materialize_conversation_memory_summary_exact(summary)
        )
        replay = asyncio.run(
            storage.materialize_conversation_memory_summary_exact(summary)
        )
        self.assertEqual(first, summary)
        self.assertEqual(replay, summary)

        drift_values = {
            "covered_until_turn_id": "task-other",
            "covered_until_message_id": "message-other",
            "covered_until_created_at": summary.covered_until_created_at
            + timedelta(seconds=1),
            "summary_text": "changed",
            "source_message_count": summary.source_message_count + 1,
            "source_message_ids_hash": "hash-other",
            "estimated_tokens": summary.estimated_tokens + 1,
            "summary_version": "v2",
            "compression_policy_version": "policy-v2",
            "model_metadata_safe": {"provider": "other"},
            "last_error": "changed",
            "created_at": summary.created_at + timedelta(seconds=1),
            "updated_at": summary.updated_at + timedelta(seconds=1),
        }
        for field_name, value in drift_values.items():
            with self.subTest(field_name=field_name):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "conversation_memory_summary_materialization_conflict",
                ):
                    asyncio.run(
                        storage.materialize_conversation_memory_summary_exact(
                            replace(summary, **{field_name: value})
                        )
                    )

        self.assertEqual(
            asyncio.run(storage.get_conversation_memory_summary(summary.summary_id)),
            summary,
        )

    def test_exact_summary_requires_existing_owned_conversation(self) -> None:
        storage = SQLiteStorage(self.session_factory)
        summary = _exact_summary()
        asyncio.run(
            storage.save_conversation(
                Conversation(conversation_id="conv-1", username="alice")
            )
        )

        for candidate in (
            replace(summary, conversation_id="missing"),
            replace(summary, username="bob"),
        ):
            with self.assertRaisesRegex(RuntimeError, "conversation_not_available"):
                asyncio.run(
                    storage.materialize_conversation_memory_summary_exact(
                        candidate
                    )
                )
        with self.session_factory() as session:
            self.assertEqual(session.query(ConversationMemorySummaryRow).count(), 0)

    def test_exact_summary_rejects_deleting_conversation(self) -> None:
        storage = SQLiteStorage(self.session_factory)
        summary = _exact_summary()
        asyncio.run(
            storage.save_conversation(
                Conversation(
                    conversation_id="conv-1",
                    username="alice",
                    status=ConversationStatus.DELETING,
                )
            )
        )

        with self.assertRaisesRegex(RuntimeError, "conversation_not_available"):
            asyncio.run(
                storage.materialize_conversation_memory_summary_exact(summary)
            )

    def test_exact_summary_write_fault_rolls_back_without_orphan(self) -> None:
        storage = SQLiteStorage(self.session_factory)
        summary = _exact_summary()
        asyncio.run(
            storage.save_conversation(
                Conversation(conversation_id="conv-1", username="alice")
            )
        )
        original_flush = Session.flush

        def fail_summary(session: Session, *args: object, **kwargs: object) -> None:
            if any(
                isinstance(row, ConversationMemorySummaryRow)
                for row in session.new
            ):
                raise RuntimeError("summary-write-fault")
            original_flush(session, *args, **kwargs)

        with patch.object(Session, "flush", new=fail_summary):
            with self.assertRaisesRegex(RuntimeError, "summary-write-fault"):
                asyncio.run(
                    storage.materialize_conversation_memory_summary_exact(
                        summary
                    )
                )
        with self.session_factory() as session:
            self.assertEqual(session.query(ConversationMemorySummaryRow).count(), 0)
            conversation = session.get(ConversationRow, "conv-1")
            self.assertIsNotNone(conversation)
            self.assertEqual(conversation.status, str(ConversationStatus.ACTIVE))

    def test_legacy_summary_save_keeps_merge_semantics(self) -> None:
        storage = SQLiteStorage(self.session_factory)
        summary = _exact_summary()
        asyncio.run(storage.save_conversation_memory_summary(summary))
        changed = replace(summary, summary_text="legacy update")

        self.assertEqual(
            asyncio.run(storage.save_conversation_memory_summary(changed)),
            changed,
        )
        self.assertEqual(
            asyncio.run(storage.get_conversation_memory_summary(summary.summary_id)),
            changed,
        )

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


def _exact_summary() -> ConversationMemorySummary:
    now = datetime(2026, 8, 26, 8, 0, 0)
    return ConversationMemorySummary(
        summary_id="summary-exact",
        conversation_id="conv-1",
        username="alice",
        covered_until_turn_id="task-1",
        covered_until_message_id="message-1",
        covered_until_created_at=now,
        summary_text="deterministic summary",
        source_message_count=4,
        source_message_ids_hash="hash-1",
        estimated_tokens=20,
        summary_version="v1",
        compression_policy_version="policy-v1",
        model_metadata_safe={"provider": "fake", "model": "test"},
        last_error=None,
        created_at=now,
        updated_at=now,
    )
