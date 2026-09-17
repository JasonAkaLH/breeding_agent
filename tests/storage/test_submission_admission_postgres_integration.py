from __future__ import annotations

import asyncio
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from sqlalchemy import delete, select

from src.api.runtime import ApiRuntime
from src.core.enums import ConversationStatus
from src.core.models import (
    Conversation,
    SubmissionAdmissionDisposition,
    SubmissionProjectionAcknowledgementRequest,
)
from src.storage.postgres import (
    bootstrap_postgres_database,
    create_postgres_engine,
    create_postgres_session_factory,
)
from src.storage.postgres.repositories import PostgreSQLStorage
from src.storage.sqlalchemy_models import (
    ConversationRow,
    EventRecordRow,
    MessageRow,
    TaskRow,
)
from src.storage.sqlite.repositories import SQLiteStateRepository
from tests.postgres_test_support import isolated_postgres_test_dsn_or_skip_reason
from tests.storage.test_submission_admission_sqlite import (
    _FakeSubmissionSidecar,
    _request,
)


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
                delete(EventRecordRow).where(EventRecordRow.task_id.like(f"{self.task_id}%"))
            )
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

    async def test_route_materialization_uses_naive_postgres_task_time(self) -> None:
        request = _request(
            conversation_id=self.conversation_id,
            message_id=self.message_id,
            task_id=self.task_id,
        )
        admitted = await self.storage.admit_submission(request)
        self.assertIsNotNone(admitted.record)
        assert admitted.record is not None

        task = await self.storage.get_task(self.task_id)
        self.assertIsNotNone(task)
        assert task is not None
        self.assertIsNone(task.created_at.tzinfo)
        self.assertEqual(task.created_at, admitted.record.created_at)

        runtime = object.__new__(ApiRuntime)
        runtime.storage = self.storage
        runtime.event_broker = SimpleNamespace(publish=AsyncMock())
        runtime._mcp_audit_reference_signer = SimpleNamespace(
            safe_owner_reference=lambda *_args, **_kwargs: "safe-owner"
        )
        runtime._mcp_rollout_metric_recorder = None

        await runtime.materialize_route_decision(admitted.record, b"{}")

        events = await self.storage.list_events_for_task(self.task_id)
        self.assertEqual(
            {event.event_type for event in events},
            {"task.accepted", "mcp.rollout.route_assigned"},
        )

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

    async def test_enforce_projection_and_replay_never_write_sql_task(self) -> None:
        sidecar = _FakeSubmissionSidecar()
        storage = PostgreSQLStorage(
            self.session_factory,
            runtime_sidecar_client=sidecar,
            mcp_task_authority_mode="enforce",
        )
        request = _request(
            conversation_id=self.conversation_id,
            message_id=self.message_id,
            task_id=self.task_id,
        )

        admitted = await storage.admit_submission(request)
        self.assertIsNotNone(admitted.handle)
        assert admitted.handle is not None
        phase = await storage.acknowledge_submission_projection(
            SubmissionProjectionAcknowledgementRequest(
                handle=admitted.handle,
                projection_sha256=request.projection_sha256,
                acknowledged_at=request.message_created_at,
            )
        )
        replay = await storage.admit_submission(request)

        self.assertEqual(str(phase.projection_state), "projected")
        self.assertEqual(
            replay.disposition,
            SubmissionAdmissionDisposition.IDEMPOTENT_REPLAY,
        )
        self.assertEqual(replay.task_id, self.task_id)
        with self.session_factory() as session:
            self.assertIsNotNone(session.get(ConversationRow, self.conversation_id))
            message = session.get(MessageRow, self.message_id)
            self.assertIsNotNone(message)
            self.assertEqual(str(message.role), "user")
            self.assertIsNone(session.get(TaskRow, self.task_id))

    async def test_marked_deleting_row_blocks_waiting_admission_projection(self) -> None:
        request = _request(
            conversation_id=self.conversation_id,
            message_id=self.message_id,
            task_id=self.task_id,
        )
        await self.storage.save_conversation(
            Conversation(
                conversation_id=self.conversation_id,
                username=request.username,
                created_at=request.message_created_at.replace(tzinfo=None),
                updated_at=request.message_created_at.replace(tzinfo=None),
            )
        )
        row_locked = threading.Event()
        release_row = threading.Event()

        def mark_deleting_while_locked() -> None:
            with self.session_factory() as session:
                row = session.scalar(
                    select(ConversationRow)
                    .where(
                        ConversationRow.conversation_id == self.conversation_id
                    )
                    .with_for_update()
                )
                assert row is not None
                row_locked.set()
                if not release_row.wait(timeout=5):
                    raise RuntimeError("postgres row-lock test timed out")
                row.status = str(ConversationStatus.DELETING)
                session.commit()

        marker = asyncio.create_task(asyncio.to_thread(mark_deleting_while_locked))
        self.assertTrue(await asyncio.to_thread(row_locked.wait, 5))
        admission = asyncio.create_task(self.storage.admit_submission(request))
        try:
            await asyncio.sleep(0.05)
            self.assertFalse(admission.done())
        finally:
            release_row.set()
        await marker
        result = await admission

        self.assertEqual(
            result.disposition,
            SubmissionAdmissionDisposition.CONVERSATION_NOT_AVAILABLE,
        )
        with self.session_factory() as session:
            self.assertIsNone(session.get(MessageRow, self.message_id))
            self.assertIsNone(session.get(TaskRow, self.task_id))
