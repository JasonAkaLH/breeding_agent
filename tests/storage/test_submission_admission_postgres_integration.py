from __future__ import annotations

import asyncio
import threading
import unittest
from datetime import timedelta
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from sqlalchemy import delete, select

from src.api.runtime import ApiRuntime
from src.api.submission_admission import SubmissionAdmissionCoordinator
from src.core.enums import ConversationStatus, TaskStatus
from src.core.models import (
    Conversation,
    SubmissionAdmissionDisposition,
    SubmissionClaimRequest,
    SubmissionFailureRequest,
    SubmissionProjectionAcknowledgementRequest,
    TaskNode,
)
from src.storage.postgres import (
    PostgreSQLAgentRepository,
    bootstrap_postgres_database,
    create_postgres_engine,
    create_postgres_session_factory,
)
from src.storage.postgres.repositories import PostgreSQLStorage
from src.storage.sqlalchemy_models import (
    AgentRunRow,
    ConversationRow,
    EventRecordRow,
    MessageRow,
    TaskRow,
    TaskNodeRow,
)
from src.storage.sqlite.repositories import SQLiteStateRepository
from src.orchestration.agent_loop.models import (
    AgentModelBinding,
    AgentRun,
    AgentRunStatus,
    AgentStorageConflict,
)
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

    async def test_expired_claim_has_one_winner_across_independent_workers(
        self,
    ) -> None:
        request = _request(
            conversation_id=self.conversation_id,
            message_id=self.message_id,
            task_id=self.task_id,
        )
        admitted = await self.storage.admit_submission(request)
        other = PostgreSQLStorage(self.session_factory, mcp_task_authority_mode="off")
        later = request.claim_expires_at + timedelta(seconds=1)
        claims = await asyncio.gather(
            *(
                storage.claim_pending_submission(
                    SubmissionClaimRequest(
                        claim_owner=owner,
                        now=later,
                        claim_expires_at=later + timedelta(seconds=30),
                    )
                )
                for storage, owner in (
                    (self.storage, "recovery-a"),
                    (other, "recovery-b"),
                )
            )
        )
        self.assertEqual(sum(claim.found for claim in claims), 1)
        with self.assertRaisesRegex(RuntimeError, "submission_claim_invalid"):
            await self.storage.fail_submission(
                SubmissionFailureRequest(handle=admitted.handle, failed_at=later)
            )
        winner = next(i for i, claim in enumerate(claims) if claim.found)
        storage = (self.storage, other)[winner]
        self.assertTrue(
            (
                await storage.fail_submission(
                    SubmissionFailureRequest(
                        handle=claims[winner].handle, failed_at=later
                    )
                )
            ).failed
        )
        self.assertEqual(
            (await self.storage.get_task(self.task_id)).status, TaskStatus.FAILED
        )
        self.assertIsNone(
            (await self.storage.get_conversation(self.conversation_id)).current_task_id
        )
        replay = await other.admit_submission(
            _request(
                conversation_id=self.conversation_id,
                message_id=self.message_id,
                task_id=f"{self.task_id}-retry",
            )
        )
        self.assertEqual(replay.task_id, self.task_id)
        self.assertIsNone(replay.handle)
        self.assertEqual(len(await self.storage.list_events_for_task(self.task_id)), 1)

    async def test_failure_frees_guard_before_new_submission_and_keeps_original_snapshot(
        self,
    ) -> None:
        request = _request(
            conversation_id=self.conversation_id,
            message_id=self.message_id,
            task_id=self.task_id,
        )
        admitted = await self.storage.admit_submission(request)
        await self.storage.fail_submission(
            SubmissionFailureRequest(
                handle=admitted.handle, failed_at=request.message_created_at
            )
        )
        second = _request(
            conversation_id=self.conversation_id,
            message_id=f"{self.message_id}-next",
            task_id=f"{self.task_id}-next",
        )
        accepted = await self.storage.admit_submission(second)
        self.assertEqual(accepted.disposition, SubmissionAdmissionDisposition.CREATED)
        self.assertEqual(
            (await self.storage.get_conversation(self.conversation_id)).current_task_id,
            second.task.task_id,
        )
        replay = await self.storage.admit_submission(request)
        self.assertEqual(replay.task_id, request.task.task_id)
        self.assertEqual(
            (await self.storage.get_conversation(self.conversation_id)).current_task_id,
            second.task.task_id,
        )

    async def asyncTearDown(self) -> None:
        with self.session_factory() as session:
            session.execute(delete(TaskNodeRow).where(TaskNodeRow.task_id.like(f"{self.task_id}%")))
            session.execute(
                delete(AgentRunRow).where(AgentRunRow.task_id.like(f"{self.task_id}%"))
            )
            session.execute(
                delete(EventRecordRow).where(
                    EventRecordRow.task_id.like(f"{self.task_id}%")
                )
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
                    ConversationRow.conversation_id.like(f"{self.conversation_id}%")
                )
            )
            session.commit()

    async def test_startup_rescans_when_candidate_closes_before_claim_lock(
        self,
    ) -> None:
        request = _request(
            conversation_id=self.conversation_id,
            message_id=self.message_id,
            task_id=self.task_id,
        )
        admitted = await self.storage.admit_submission(request)
        recovery = PostgreSQLStorage(
            self.session_factory, mcp_task_authority_mode="off"
        )
        now = request.message_created_at + timedelta(seconds=1)
        selected = threading.Event()
        proceed = threading.Event()
        claim = SQLiteStateRepository.claim_submission_sql

        def pause_after_selection(repo, **kwargs):
            if kwargs["claim_owner"] == "startup-worker" and not selected.is_set():
                selected.set()
                if not proceed.wait(5):
                    raise AssertionError("claim interleaving timed out")
            return claim(repo, **kwargs)

        coordinator = SubmissionAdmissionCoordinator(
            admission=recovery,
            receipts=SimpleNamespace(),
            callbacks=SimpleNamespace(),
            claim_owner="startup-worker",
            now=lambda: now,
            wait_until=AsyncMock(),
            expected_finalization_receipt_sha256=None,
        )
        with patch.object(
            SQLiteStateRepository, "claim_submission_sql", pause_after_selection
        ):
            startup = asyncio.create_task(coordinator.project_pending())
            try:
                self.assertTrue(await asyncio.to_thread(selected.wait, 5))
                self.assertTrue(
                    (
                        await self.storage.fail_submission(
                            SubmissionFailureRequest(
                                handle=admitted.handle, failed_at=now
                            )
                        )
                    ).failed
                )
            finally:
                proceed.set()
            result = await startup
        self.assertEqual(result.pending_count, 0)
        self.assertEqual(result.recovered_count, 0)
        await coordinator.abort_pending()

    async def test_failure_and_preparation_node_creation_leave_no_running_node(
        self,
    ) -> None:
        request = _request(
            conversation_id=self.conversation_id,
            message_id=self.message_id,
            task_id=self.task_id,
        )
        admitted = await self.storage.admit_submission(request)
        node = TaskNode(
            node_id=f"{self.task_id}:file_selection",
            task_id=self.task_id,
            capability_id="agent.file_selection",
            status="running",
        )
        created, failed = await asyncio.gather(
            self.storage.save_task_node(node),
            self.storage.fail_submission(
                SubmissionFailureRequest(
                    handle=admitted.handle,
                    failed_at=request.message_created_at,
                )
            ),
            return_exceptions=True,
        )
        self.assertNotIsInstance(failed, BaseException)
        self.assertTrue(failed.failed)
        if isinstance(created, BaseException):
            self.assertIn("submission_preparation_closed", str(created))
        nodes = await self.storage.list_task_nodes_for_task(self.task_id)
        self.assertTrue(
            all(
                str(item.status) == "failed" and item.finished_at is not None
                for item in nodes
            )
        )

    async def test_failure_projection_markers_are_private_paged_and_owner_bound(
        self,
    ) -> None:
        for suffix in ("", "-next"):
            request = _request(
                conversation_id=f"{self.conversation_id}{suffix}",
                message_id=f"{self.message_id}{suffix}",
                task_id=f"{self.task_id}{suffix}",
            )
            admitted = await self.storage.admit_submission(request)
            await self.storage._run(
                lambda state, collab: state.begin_submission_failure_projection(
                    admitted.record
                )
            )
            message = await self.storage.get_message(request.message_id)
            self.assertEqual(message.metadata, {"visible": "yes"})
            await self.storage.save_message(message)
        first = await self.storage.list_pending_submission_failure_projections(limit=1)
        self.assertEqual([item.message_id for item in first], [self.message_id])
        second = await self.storage.list_pending_submission_failure_projections(
            after_message_id=first[0].message_id, limit=1
        )
        self.assertEqual(
            [item.message_id for item in second], [f"{self.message_id}-next"]
        )
        with self.assertRaisesRegex(RuntimeError, "identity_mismatch"):
            await self.storage.complete_submission_failure_projection(
                replace(first[0], username="another-owner")
            )
        await self.storage.complete_submission_failure_projection(first[0])
        self.assertEqual(
            await self.storage.list_pending_submission_failure_projections(), second
        )
        await self.storage.complete_submission_failure_projection(second[0])
        self.assertEqual(
            await self.storage.list_pending_submission_failure_projections(), []
        )

    async def test_run_creation_and_failure_cannot_commit_conflicting_outcomes(
        self,
    ) -> None:
        request = _request(
            conversation_id=self.conversation_id,
            message_id=self.message_id,
            task_id=self.task_id,
        )
        admitted = await self.storage.admit_submission(request)
        repository = PostgreSQLAgentRepository(self.session_factory)
        run = AgentRun(
            run_id=f"agent-run:{self.task_id}",
            task_id=self.task_id,
            conversation_id=self.conversation_id,
            status=AgentRunStatus.RUNNING,
            binding=AgentModelBinding(model_edition="test"),
        )
        created, failed = await asyncio.gather(
            repository.create_run(run),
            self.storage.fail_submission(
                SubmissionFailureRequest(
                    handle=admitted.handle,
                    failed_at=request.message_created_at,
                )
            ),
            return_exceptions=True,
        )
        self.assertNotIsInstance(failed, BaseException)
        actual_run = await repository.get_run_for_task(self.task_id)
        task = await self.storage.get_task(self.task_id)
        if failed.failed:
            self.assertIsInstance(created, AgentStorageConflict)
            self.assertIsNone(actual_run)
            self.assertEqual(task.status, TaskStatus.FAILED)
            with self.assertRaisesRegex(
                AgentStorageConflict, "agent_run_task_terminal"
            ):
                await repository.create_run(run)
        else:
            self.assertIsInstance(created, AgentRun)
            self.assertIsNotNone(actual_run)
            self.assertEqual(task.status, TaskStatus.ACCEPTED)

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
