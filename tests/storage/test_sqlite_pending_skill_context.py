from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from src.core.models import Conversation, PendingSkillContext
from src.storage.sqlite import SQLiteStorage
from src.storage.sqlite.repositories import SQLiteStateRepository
from tests.storage.support import SQLiteStorageTestCase


class SQLitePendingSkillContextRepositoryTest(SQLiteStorageTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.storage = SQLiteStorage(self.session_factory)
        asyncio.run(
            self.storage.save_conversation(
                Conversation(conversation_id="conv-1", username="acc-1")
            )
        )

    def _context(self, context_id: str = "ctx-1", *, conversation_id: str = "conv-1") -> PendingSkillContext:
        return PendingSkillContext(
            context_id=context_id,
            conversation_id=conversation_id,
            username="acc-1",
            capability_id="skill.need_variety",
            skill_name="need-variety",
            source_task_id="task-1",
            source_message_id="msg-1",
            original_user_message="请查询",
            missing_requirements=("variety",),
            assistant_message="缺少 Skill 脚本必需参数：variety。请补充后继续。",
            status="pending_user_input",
            created_at=datetime(2026, 5, 20, 6, 0, 0),
            updated_at=datetime(2026, 5, 20, 6, 0, 0),
        )

    def test_pending_context_round_trip_and_status_transitions(self) -> None:
        context = self._context()
        with self.session_factory() as session:
            repo = SQLiteStateRepository(session)
            saved = repo.save_pending_skill_context(context)
            active = repo.get_active_pending_skill_context("conv-1")
            repo.mark_pending_skill_context_consumed("ctx-1", updated_at=datetime(2026, 5, 20, 6, 5, 0))
            consumed = repo.get_pending_skill_context("ctx-1")
            session.commit()

        self.assertEqual(saved, context)
        self.assertEqual(active, context)
        self.assertIsNotNone(consumed)
        self.assertEqual(consumed.status, "consumed")
        self.assertIsNone(consumed.updated_at.tzinfo if consumed.updated_at else None)

    def test_saving_new_active_context_supersedes_existing_context_for_conversation(self) -> None:
        first = self._context("ctx-1")
        second = self._context("ctx-2", conversation_id="conv-1")
        with self.session_factory() as session:
            repo = SQLiteStateRepository(session)
            repo.save_pending_skill_context(first)
            repo.save_pending_skill_context(second)
            active = repo.get_active_pending_skill_context("conv-1")
            old = repo.get_pending_skill_context("ctx-1")
            session.commit()

        self.assertEqual(active.context_id, "ctx-2")
        self.assertEqual(old.status, "superseded")

    def test_async_facade_exposes_pending_context_methods(self) -> None:
        context = self._context()

        saved = asyncio.run(self.storage.save_pending_skill_context(context))
        active = asyncio.run(self.storage.get_active_pending_skill_context("conv-1"))
        consumed = asyncio.run(self.storage.mark_pending_skill_context_consumed("ctx-1"))

        self.assertEqual(saved.context_id, "ctx-1")
        self.assertEqual(active.context_id, "ctx-1")
        self.assertEqual(consumed.status, "consumed")

    def test_prepared_pending_context_is_consumed_once_with_exact_receipt(self) -> None:
        context = self._context()
        asyncio.run(self.storage.save_pending_skill_context(context))
        occurred_at = datetime(2026, 5, 20, 6, 5, 0)

        first, duplicate = asyncio.run(
            self.storage.materialize_submission_pending_skill_transition_exact(
                username="acc-1",
                conversation_id="conv-1",
                task_id="task-current",
                prepared_execution_sha256="a" * 64,
                target_status="consumed",
                reason="legacy_pending_continued",
                pending_context=context,
                occurred_at=occurred_at,
            )
        )
        replay, replay_duplicate = asyncio.run(
            self.storage.materialize_submission_pending_skill_transition_exact(
                username="acc-1",
                conversation_id="conv-1",
                task_id="task-current",
                prepared_execution_sha256="a" * 64,
                target_status="consumed",
                reason="legacy_pending_continued",
                pending_context=context,
                occurred_at=occurred_at,
            )
        )

        self.assertFalse(duplicate)
        self.assertTrue(replay_duplicate)
        self.assertEqual(replay, first)
        self.assertEqual(first.event_type, "pending_skill_context.consumed")
        self.assertEqual(first.payload["count"], 1)
        self.assertEqual(
            asyncio.run(self.storage.get_pending_skill_context("ctx-1")).status,
            "consumed",
        )
        with self.assertRaisesRegex(
            RuntimeError, "pending_skill_transition_context_conflict"
        ):
            asyncio.run(
                self.storage.materialize_submission_pending_skill_transition_exact(
                    username="acc-1",
                    conversation_id="conv-1",
                    task_id="different-task",
                    prepared_execution_sha256="b" * 64,
                    target_status="consumed",
                    reason="legacy_pending_continued",
                    pending_context=context,
                    occurred_at=occurred_at + timedelta(seconds=1),
                )
            )
