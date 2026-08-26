from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import delete

from src.core.models import SubmissionAdmissionDisposition
from src.storage.postgres import (
    bootstrap_postgres_database,
    create_postgres_engine,
    create_postgres_session_factory,
)
from src.storage.postgres.repositories import PostgreSQLStorage
from src.storage.sqlalchemy_models import ConversationRow, MessageRow, TaskRow
from src.storage.sqlite.repositories import SQLiteStateRepository
from tests.postgres_test_support import isolated_postgres_test_dsn_or_skip_reason
from tests.storage.test_submission_admission_sqlite import _request


class SubmissionAdmissionPostgresIntegrationTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        dsn, skip_reason = isolated_postgres_test_dsn_or_skip_reason(
            "MAF_POSTGRES_SUBMISSION_ADMISSION_TEST_DSN",
            fallback_env="MAF_POSTGRES_TEST_DSN",
        )
        if skip_reason:
            raise unittest.SkipTest(skip_reason)
        assert dsn is not None
        cls.engine = create_postgres_engine(dsn, pool_size=4, max_overflow=0)
        bootstrap_postgres_database(cls.engine)
        cls.session_factory = create_postgres_session_factory(cls.engine)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.dispose()

    async def asyncSetUp(self) -> None:
        suffix = uuid4().hex
        self.conversation_id = f"submission-pg-conversation-{suffix}"
        self.message_id = f"submission-pg-message-{suffix}"
        self.task_id = f"submission-pg-task-{suffix}"
        self.storage = PostgreSQLStorage(
            self.session_factory,
            mcp_task_authority_mode="off",
        )

    async def asyncTearDown(self) -> None:
        with self.session_factory() as session:
            session.execute(
                delete(TaskRow).where(TaskRow.task_id.like(f"{self.task_id}%"))
            )
            session.execute(
                delete(MessageRow).where(
                    MessageRow.message_id.like(f"{self.message_id}%")
                )
            )
            session.execute(
                delete(ConversationRow).where(
                    ConversationRow.conversation_id.like(
                        f"{self.conversation_id}%"
                    )
                )
            )
            session.commit()

    async def test_same_request_two_connections_converge_created_and_replay(self) -> None:
        request = _request(
            conversation_id=self.conversation_id,
            message_id=self.message_id,
            task_id=self.task_id,
        )
        results = await asyncio.gather(
            self.storage.admit_submission(request),
            self.storage.admit_submission(request),
        )

        self.assertEqual(
            {result.disposition for result in results},
            {
                SubmissionAdmissionDisposition.CREATED,
                SubmissionAdmissionDisposition.IDEMPOTENT_REPLAY,
            },
        )
        self.assertEqual({result.task_id for result in results}, {self.task_id})

    async def test_different_messages_same_conversation_have_one_created_one_busy(self) -> None:
        first = _request(
            conversation_id=self.conversation_id,
            message_id=self.message_id,
            task_id=self.task_id,
        )
        second = _request(
            conversation_id=self.conversation_id,
            message_id=f"{self.message_id}-other",
            task_id=f"{self.task_id}-other",
        )
        results = await asyncio.gather(
            self.storage.admit_submission(first),
            self.storage.admit_submission(second),
        )

        self.assertEqual(
            {result.disposition for result in results},
            {
                SubmissionAdmissionDisposition.CREATED,
                SubmissionAdmissionDisposition.CONVERSATION_BUSY,
            },
        )

    async def test_same_global_message_across_conversations_conflicts_without_orphan(self) -> None:
        first = _request(
            conversation_id=self.conversation_id,
            message_id=self.message_id,
            task_id=self.task_id,
        )
        second_conversation = f"{self.conversation_id}-other"
        second = _request(
            username="other",
            conversation_id=second_conversation,
            message_id=self.message_id,
            task_id=f"{self.task_id}-other",
        )
        results = await asyncio.gather(
            self.storage.admit_submission(first),
            self.storage.admit_submission(second),
        )

        self.assertEqual(
            {result.disposition for result in results},
            {
                SubmissionAdmissionDisposition.CREATED,
                SubmissionAdmissionDisposition.MESSAGE_ID_CONFLICT,
            },
        )
        with self.session_factory() as session:
            conversations = session.query(ConversationRow).filter(
                ConversationRow.conversation_id.in_(
                    [self.conversation_id, second_conversation]
                )
            )
            self.assertEqual(conversations.count(), 1)

    async def test_task_write_fault_rolls_back_conversation_and_message(self) -> None:
        request = _request(
            conversation_id=self.conversation_id,
            message_id=self.message_id,
            task_id=self.task_id,
        )
        with patch.object(
            SQLiteStateRepository,
            "save_task",
            side_effect=RuntimeError("postgres-task-write-fault"),
        ):
            with self.assertRaisesRegex(RuntimeError, "postgres-task-write-fault"):
                await self.storage.admit_submission(request)
        with self.session_factory() as session:
            self.assertIsNone(session.get(ConversationRow, self.conversation_id))
            self.assertIsNone(session.get(MessageRow, self.message_id))
            self.assertIsNone(session.get(TaskRow, self.task_id))
