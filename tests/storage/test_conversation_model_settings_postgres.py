from __future__ import annotations

import asyncio
import unittest
from uuid import uuid4

from sqlalchemy import select, text

from src.core.models import Conversation
from src.storage.postgres import (
    bootstrap_postgres_database,
    create_postgres_engine,
    create_postgres_session_factory,
)
from src.storage.postgres.repositories import PostgreSQLStorage
from src.storage.sqlite.repositories import SQLiteStateRepository
from src.storage.sqlalchemy_models import ConversationRow, ConversationModelSettingsRow
from tests.postgres_test_support import isolated_postgres_test_dsn_or_skip_reason
from tests.storage.test_conversation_model_settings import settings, selected_request


class ConversationModelSettingsPostgresTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        dsn, reason = isolated_postgres_test_dsn_or_skip_reason(
            "MAF_POSTGRES_MODEL_SETTINGS_TEST_DSN"
        )
        if reason:
            raise unittest.SkipTest(reason)
        cls.engine = create_postgres_engine(dsn, pool_size=5, max_overflow=0)
        bootstrap_postgres_database(cls.engine)
        cls.sessions = create_postgres_session_factory(cls.engine)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    async def asyncSetUp(self):
        self.id = "prefs-pg-" + uuid4().hex
        self.storage = PostgreSQLStorage(self.sessions)
        await self.storage.save_conversation(Conversation(self.id, "owner"))

    async def asyncTearDown(self):
        await self.storage.delete_conversation_physical(self.id)

    async def test_two_connections_same_revision_only_one_wins(self):
        results = await asyncio.gather(
            *[
                self.storage.put_conversation_model_settings(
                    settings(self.id, effort=effort),
                    username="owner",
                    expected_revision=0,
                )
                for effort in ("high", "low")
            ]
        )
        self.assertEqual(sum(result is not None for result in results), 1)
        value = await self.storage.get_conversation_model_settings(
            self.id, username="owner"
        )
        self.assertEqual(value.revision, 1)

    async def test_save_waiting_behind_delete_mark_rejects(self):
        with self.sessions() as session:
            parent = session.scalar(
                select(ConversationRow)
                .where(ConversationRow.conversation_id == self.id)
                .with_for_update()
            )
            parent.status = "deleting"
            session.flush()
            operation = asyncio.create_task(
                self.storage.put_conversation_model_settings(
                    settings(self.id), username="owner", expected_revision=0
                )
            )
            await self._wait_for_blocked_connection()
            self.assertFalse(operation.done())
            session.commit()
        with self.assertRaises(PermissionError):
            await operation
        with self.sessions() as session:
            self.assertIsNone(session.get(ConversationModelSettingsRow, self.id))

    async def test_physical_delete_waits_for_save_and_removes_late_child(self):
        with self.sessions() as session:
            repo = SQLiteStateRepository(session)
            repo.put_conversation_model_settings(
                settings(self.id), username="owner", expected_revision=0
            )
            operation = asyncio.create_task(
                self.storage.delete_conversation_physical(self.id)
            )
            await self._wait_for_blocked_connection()
            self.assertFalse(operation.done())
            session.commit()
        counts = await operation
        self.assertEqual(counts["conversation_model_settings"], 1)
        with self.sessions() as session:
            self.assertIsNone(session.get(ConversationModelSettingsRow, self.id))
            self.assertIsNone(session.get(ConversationRow, self.id))
        with self.assertRaises(PermissionError):
            await self.storage.put_conversation_model_settings(
                settings(self.id), username="owner", expected_revision=0
            )

    async def test_save_waiting_behind_physical_delete_cannot_resurrect(self):
        with self.sessions() as session:
            session.scalar(
                select(ConversationRow)
                .where(ConversationRow.conversation_id == self.id)
                .with_for_update()
            )
            operation = asyncio.create_task(
                self.storage.put_conversation_model_settings(
                    settings(self.id), username="owner", expected_revision=0
                )
            )
            await self._wait_for_blocked_connection()
            session.execute(
                text("DELETE FROM conversation WHERE conversation_id = :id"),
                {"id": self.id},
            )
            session.commit()
        with self.assertRaises(PermissionError):
            await operation
        with self.sessions() as session:
            self.assertIsNone(session.get(ConversationModelSettingsRow, self.id))

    async def test_bootstrap_adds_missing_table_without_backfill(self):
        # This test owns an isolated test database; it never uses production DSNs.
        ConversationModelSettingsRow.__table__.drop(self.engine)
        bootstrap_postgres_database(self.engine)
        self.assertIsNone(
            await self.storage.get_conversation_model_settings(
                self.id, username="owner"
            )
        )

    async def test_first_admission_and_replay_preserve_later_settings(self):
        await self.storage.delete_conversation_physical(self.id)
        request = selected_request(
            conversation_id=self.id,
            message_id="msg-" + uuid4().hex,
            task_id="task-" + uuid4().hex,
        )
        await self.storage.admit_submission(request)
        original = await self.storage.get_conversation_model_settings(
            self.id, username="owner"
        )
        self.assertTrue(original.deep_thinking)
        edited = await self.storage.put_conversation_model_settings(
            settings(self.id, effort="low"), username="owner", expected_revision=1
        )
        await self.storage.admit_submission(request)
        self.assertEqual(
            await self.storage.get_conversation_model_settings(
                self.id, username="owner"
            ),
            edited,
        )

    async def _wait_for_blocked_connection(self):
        # Observe a real PostgreSQL lock wait, rather than guessing with sleeps.
        for _ in range(200):
            with self.engine.connect() as connection:
                blocked = connection.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND wait_event_type = 'Lock' AND pid <> pg_backend_pid()"
                    )
                )
            if blocked:
                return
            await asyncio.sleep(0.01)
        self.fail("second PostgreSQL connection did not reach the row lock")
