from __future__ import annotations

import asyncio
import unittest
from datetime import datetime
from uuid import uuid4

from sqlalchemy import delete

from src.core.enums import InterruptStatus
from src.core.models import Interrupt, InterruptAnswer
from src.lifecycle.errors import LifecycleTransitionError
from src.storage.postgres import (
    bootstrap_postgres_database,
    create_postgres_engine,
    create_postgres_session_factory,
)
from src.storage.postgres.repositories import PostgreSQLStorage
from src.storage.sqlalchemy_models import InterruptAnswerRow, InterruptRow
from tests.postgres_test_support import isolated_postgres_test_dsn_or_skip_reason


class InterruptAnswerPostgresIntegrationTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        dsn, skip_reason = isolated_postgres_test_dsn_or_skip_reason(
            "MAF_POSTGRES_INTERRUPT_ANSWER_TEST_DSN",
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
        self.interrupt_id = f"interrupt-answer-pg-{suffix}"
        self.storage = PostgreSQLStorage(
            self.session_factory,
            mcp_task_authority_mode="off",
        )
        await self.storage.save_interrupt(
            Interrupt(
                interrupt_id=self.interrupt_id,
                conversation_id=f"conversation-answer-pg-{suffix}",
                task_id=f"task-answer-pg-{suffix}",
                node_id=f"node-answer-pg-{suffix}",
                source_agent="skill.data-query",
                source_message_id="mail-1",
                question="region?",
                reason_code="missing_region",
            )
        )

    async def asyncTearDown(self) -> None:
        with self.session_factory() as session:
            session.execute(
                delete(InterruptAnswerRow).where(
                    InterruptAnswerRow.interrupt_id == self.interrupt_id
                )
            )
            session.execute(
                delete(InterruptRow).where(
                    InterruptRow.interrupt_id == self.interrupt_id
                )
            )
            session.commit()

    async def test_two_connections_have_one_final_answer_winner(self) -> None:
        answers = (
            InterruptAnswer(
                interrupt_answer_id=f"{self.interrupt_id}:east",
                interrupt_id=self.interrupt_id,
                answer_payload={"region": "east"},
                source_message_id="message-east",
            ),
            InterruptAnswer(
                interrupt_answer_id=f"{self.interrupt_id}:west",
                interrupt_id=self.interrupt_id,
                answer_payload={"region": "west"},
                source_message_id="message-west",
            ),
        )

        async def claim(index: int) -> object:
            answer = answers[index]
            return await self.storage._run(
                lambda state, collab: collab.claim_split_interrupt_answer_final(
                    answer,
                    now=datetime(2026, 8, 27, 10, 0, index),
                    allow_create=True,
                )
            )

        results = await asyncio.gather(
            claim(0),
            claim(1),
            return_exceptions=True,
        )

        self.assertEqual(
            sum(isinstance(result, LifecycleTransitionError) for result in results),
            1,
        )
        self.assertEqual(
            sum(isinstance(result, tuple) for result in results),
            1,
        )
        stored_interrupt = await self.storage.get_interrupt(self.interrupt_id)
        stored_answers = await self.storage.list_interrupt_answers(
            self.interrupt_id
        )
        self.assertEqual(stored_interrupt.status, InterruptStatus.ANSWERED)
        self.assertEqual(len(stored_answers), 1)
        self.assertEqual(stored_interrupt.answered_at, stored_answers[0].accepted_at)


if __name__ == "__main__":
    unittest.main()
