from __future__ import annotations

import asyncio
import hashlib
import unittest
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import delete, select

from src.core.models import (
    Conversation,
    MCPInitialIntentCreateResult,
    MCPNoServerConvergenceResult,
    PendingSkillContext,
    SubmissionPreparationReceiptComponent,
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
    MCPNoServerIntentRow,
    PendingSkillContextRow,
    SubmissionPreparationReceiptRow,
    TaskRow,
)
from tests.postgres_test_support import isolated_postgres_test_dsn_or_skip_reason
from tests.storage.test_submission_preparation_receipt_sqlite import _canonical


class SubmissionPreparationReceiptPostgresIntegrationTest(
    unittest.IsolatedAsyncioTestCase
):
    @classmethod
    def setUpClass(cls) -> None:
        dsn, skip_reason = isolated_postgres_test_dsn_or_skip_reason(
            "MAF_POSTGRES_SUBMISSION_PREPARATION_TEST_DSN",
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
        self.conversation_id = f"preparation-pg-conversation-{suffix}"
        self.task_id = f"preparation-pg-task-{suffix}"
        self.username = f"preparation-pg-owner-{suffix}"
        self.now = datetime(2026, 8, 26, 8, 0, tzinfo=timezone.utc)
        self.storage = PostgreSQLStorage(self.session_factory)
        await self.storage.save_conversation(
            Conversation(
                conversation_id=self.conversation_id,
                username=self.username,
                created_at=self.now,
                updated_at=self.now,
            )
        )
    async def asyncTearDown(self) -> None:
        with self.session_factory() as session:
            session.execute(
                delete(PendingSkillContextRow).where(
                    PendingSkillContextRow.conversation_id == self.conversation_id
                )
            )
            session.execute(
                delete(EventRecordRow).where(EventRecordRow.task_id == self.task_id)
            )
            session.execute(
                delete(MCPNoServerIntentRow).where(
                    MCPNoServerIntentRow.task_id == self.task_id
                )
            )
            session.execute(
                delete(SubmissionPreparationReceiptRow).where(
                    SubmissionPreparationReceiptRow.conversation_id
                    == self.conversation_id
                )
            )
            session.execute(
                delete(ConversationRow).where(
                    ConversationRow.conversation_id == self.conversation_id
                )
            )
            session.commit()

    async def test_two_connections_exact_write_converges_and_closes(self) -> None:
        route = _canonical({"decision": "agent_run"})
        writes = await asyncio.gather(
            self._write(SubmissionPreparationReceiptComponent.ROUTE_DECISION, route),
            self._write(SubmissionPreparationReceiptComponent.ROUTE_DECISION, route),
        )
        self.assertEqual(writes[0], writes[1])
        await self._write(SubmissionPreparationReceiptComponent.MEMORY_CONTEXT, b"null")
        await self._write(
            SubmissionPreparationReceiptComponent.SELECTOR_DECISION,
            _canonical({"selected": []}),
        )
        closed = await self.storage.close_submission_preparation_receipt(
            username=self.username,
            conversation_id=self.conversation_id,
            task_id=self.task_id,
            closed_at=self.now + timedelta(seconds=3),
        )
        self.assertIsNotNone(closed.receipt_sha256)

    async def test_two_connections_different_write_has_one_winner(self) -> None:
        first = _canonical({"decision": "first"})
        second = _canonical({"decision": "second"})
        results = await asyncio.gather(
            self._write(SubmissionPreparationReceiptComponent.ROUTE_DECISION, first),
            self._write(SubmissionPreparationReceiptComponent.ROUTE_DECISION, second),
            return_exceptions=True,
        )
        self.assertEqual(sum(not isinstance(result, Exception) for result in results), 1)
        self.assertEqual(
            sum(
                isinstance(result, RuntimeError)
                and "submission_preparation_receipt_conflict" in str(result)
                for result in results
            ),
            1,
        )

    async def test_physical_delete_removes_receipt_without_sql_task(self) -> None:
        await self._write(
            SubmissionPreparationReceiptComponent.ROUTE_DECISION,
            _canonical({"decision": "agent_run"}),
        )
        counts = await self.storage.delete_conversation_physical(
            self.conversation_id
        )
        self.assertEqual(counts["submission_preparation_receipts"], 1)
        with self.session_factory() as session:
            self.assertIsNone(
                session.get(SubmissionPreparationReceiptRow, self.task_id)
            )

    async def test_no_server_route_intent_and_convergence_replay_without_task(self) -> None:
        first = await self.storage.settle_submission_route_decision_exact(
            username=self.username,
            conversation_id=self.conversation_id,
            task_id=self.task_id,
            requires_user_scoped_server=True,
            written_at=self.now,
        )
        replay = await self.storage.settle_submission_route_decision_exact(
            username=self.username,
            conversation_id=self.conversation_id,
            task_id=self.task_id,
            requires_user_scoped_server=True,
            written_at=self.now + timedelta(seconds=1),
        )
        self.assertEqual(first, replay)
        self.assertIn(b'"decision":"no_server"', first.route_decision)
        self.assertEqual(
            await self.storage.materialize_submission_no_server_intent_exact(
                username=self.username,
                conversation_id=self.conversation_id,
                task_id=self.task_id,
                occurred_at=self.now + timedelta(seconds=2),
            ),
            MCPInitialIntentCreateResult.CREATED_UNAVAILABLE,
        )
        converged_at = self.now + timedelta(seconds=3)
        self.assertEqual(
            await self.storage.converge_submission_no_server_without_sql_task(
                username=self.username,
                conversation_id=self.conversation_id,
                task_id=self.task_id,
                occurred_at=converged_at,
            ),
            MCPNoServerConvergenceResult.CONVERGED,
        )
        self.assertEqual(
            await self.storage.converge_submission_no_server_without_sql_task(
                username=self.username,
                conversation_id=self.conversation_id,
                task_id=self.task_id,
                occurred_at=converged_at,
            ),
            MCPNoServerConvergenceResult.ALREADY_CONVERGED,
        )
        with self.session_factory() as session:
            self.assertIsNone(session.get(TaskRow, self.task_id))
            intent = session.scalar(
                select(MCPNoServerIntentRow).where(
                    MCPNoServerIntentRow.task_id == self.task_id
                )
            )
            self.assertIsNotNone(intent)
            self.assertIsNone(intent.resume_envelope_json)

    async def test_pending_skill_supersede_is_transactional_and_exact(self) -> None:
        await self.storage.save_pending_skill_context(
            PendingSkillContext(
                context_id=f"context-{self.task_id}",
                conversation_id=self.conversation_id,
                username=self.username,
                capability_id="skill.example",
                skill_name="example",
                source_task_id="old-task",
                source_message_id="old-message",
                original_user_message="old",
                missing_requirements=("value",),
                assistant_message="need value",
                created_at=self.now,
                updated_at=self.now,
            )
        )
        occurred_at = self.now + timedelta(seconds=1)
        first, first_duplicate = await self.storage.materialize_submission_pending_skill_transition_exact(
            username=self.username,
            conversation_id=self.conversation_id,
            task_id=self.task_id,
            prepared_execution_sha256="a" * 64,
            target_status="superseded",
            reason="new_forced_capability",
            pending_context=None,
            occurred_at=occurred_at,
        )
        replay, replay_duplicate = await self.storage.materialize_submission_pending_skill_transition_exact(
            username=self.username,
            conversation_id=self.conversation_id,
            task_id=self.task_id,
            prepared_execution_sha256="a" * 64,
            target_status="superseded",
            reason="new_forced_capability",
            pending_context=None,
            occurred_at=occurred_at,
        )
        self.assertFalse(first_duplicate)
        self.assertTrue(replay_duplicate)
        self.assertEqual(replay, first)
        self.assertEqual(first.payload["count"], 1)

    async def _write(
        self,
        component: SubmissionPreparationReceiptComponent,
        value: bytes,
    ):
        return await self.storage.write_submission_preparation_component(
            username=self.username,
            conversation_id=self.conversation_id,
            task_id=self.task_id,
            component=component,
            canonical_json=value,
            component_sha256=hashlib.sha256(value).hexdigest(),
            written_at=self.now,
        )
