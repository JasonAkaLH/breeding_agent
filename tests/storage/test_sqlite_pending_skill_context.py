from __future__ import annotations

import asyncio
from datetime import datetime

from src.core.models import PendingSkillContext
from src.storage.sqlite import SQLiteStorage
from src.storage.sqlite.repositories import SQLiteStateRepository
from tests.storage.support import SQLiteStorageTestCase


class SQLitePendingSkillContextRepositoryTest(SQLiteStorageTestCase):
    def _context(self, context_id: str = "ctx-1", *, conversation_id: str = "conv-1") -> PendingSkillContext:
        return PendingSkillContext(
            context_id=context_id,
            conversation_id=conversation_id,
            account_id="acc-1",
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
        storage = SQLiteStorage(self.session_factory)
        context = self._context()

        saved = asyncio.run(storage.save_pending_skill_context(context))
        active = asyncio.run(storage.get_active_pending_skill_context("conv-1"))
        consumed = asyncio.run(storage.mark_pending_skill_context_consumed("ctx-1"))

        self.assertEqual(saved.context_id, "ctx-1")
        self.assertEqual(active.context_id, "ctx-1")
        self.assertEqual(consumed.status, "consumed")
