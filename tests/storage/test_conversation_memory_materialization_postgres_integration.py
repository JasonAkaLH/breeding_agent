from __future__ import annotations

import asyncio
import threading
import unittest
from dataclasses import replace
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import delete, func, select

from src.core.models import Conversation
from src.storage.postgres import (
    bootstrap_postgres_database,
    create_postgres_engine,
    create_postgres_session_factory,
)
from src.storage.postgres.repositories import PostgreSQLStorage
from src.storage.sqlalchemy_models import (
    ConversationMemorySummaryRow,
    ConversationRow,
)
from src.storage.sqlite.repositories import SQLiteStateRepository
from tests.postgres_test_support import isolated_postgres_test_dsn_or_skip_reason
from tests.storage.test_sqlite_conversation_memory_repository import _exact_summary


class ConversationMemoryMaterializationPostgresIntegrationTest(
    unittest.IsolatedAsyncioTestCase
):
    @classmethod
    def setUpClass(cls) -> None:
        dsn, skip_reason = isolated_postgres_test_dsn_or_skip_reason(
            "MAF_POSTGRES_CONVERSATION_MEMORY_MATERIALIZATION_TEST_DSN",
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
        self.summary = replace(
            _exact_summary(),
            summary_id=f"summary-exact-pg-{suffix}",
            conversation_id=f"conversation-exact-pg-{suffix}",
        )
        self.storage = PostgreSQLStorage(self.session_factory)
        await self.storage.save_conversation(
            Conversation(
                conversation_id=self.summary.conversation_id,
                username=self.summary.username,
            )
        )

    async def asyncTearDown(self) -> None:
        with self.session_factory() as session:
            session.execute(
                delete(ConversationMemorySummaryRow).where(
                    ConversationMemorySummaryRow.summary_id
                    == self.summary.summary_id
                )
            )
            session.execute(
                delete(ConversationRow).where(
                    ConversationRow.conversation_id
                    == self.summary.conversation_id
                )
            )
            session.commit()

    async def test_two_connections_exact_replay_converges(self) -> None:
        first, second = await asyncio.gather(
            self.storage.materialize_conversation_memory_summary_exact(
                self.summary
            ),
            self.storage.materialize_conversation_memory_summary_exact(
                self.summary
            ),
        )
        self.assertEqual(first, self.summary)
        self.assertEqual(second, self.summary)

    async def test_two_connections_drift_has_one_winner(self) -> None:
        changed = replace(self.summary, summary_text="changed")
        results = await asyncio.gather(
            self.storage.materialize_conversation_memory_summary_exact(
                self.summary
            ),
            self.storage.materialize_conversation_memory_summary_exact(changed),
            return_exceptions=True,
        )
        self.assertEqual(sum(not isinstance(result, Exception) for result in results), 1)
        self.assertEqual(
            sum(
                isinstance(result, RuntimeError)
                and "conversation_memory_summary_materialization_conflict"
                in str(result)
                for result in results
            ),
            1,
        )

    async def test_physical_delete_waits_for_materialization_and_leaves_no_orphan(
        self,
    ) -> None:
        original = SQLiteStateRepository.materialize_conversation_memory_summary_exact
        materialized = threading.Event()
        release = threading.Event()

        def hold_after_materialization(
            repository: SQLiteStateRepository,
            summary,
        ):
            result = original(repository, summary)
            materialized.set()
            if not release.wait(timeout=5):
                raise RuntimeError("materialization-test-release-timeout")
            return result

        with patch.object(
            SQLiteStateRepository,
            "materialize_conversation_memory_summary_exact",
            new=hold_after_materialization,
        ):
            materialize_task = asyncio.create_task(
                self.storage.materialize_conversation_memory_summary_exact(
                    self.summary
                )
            )
            self.assertTrue(await asyncio.to_thread(materialized.wait, 5))
            delete_task = asyncio.create_task(
                self.storage.delete_conversation_physical(
                    self.summary.conversation_id
                )
            )
            try:
                with self.assertRaises(asyncio.TimeoutError):
                    await asyncio.wait_for(
                        asyncio.shield(delete_task),
                        timeout=0.2,
                    )
            finally:
                release.set()
            await materialize_task
            counts = await delete_task

        self.assertEqual(counts["conversation_memory_summary"], 1)
        with self.session_factory() as session:
            self.assertEqual(
                session.scalar(
                    select(func.count()).select_from(
                        ConversationMemorySummaryRow
                    )
                ),
                0,
            )
            self.assertIsNone(
                session.get(ConversationRow, self.summary.conversation_id)
            )
