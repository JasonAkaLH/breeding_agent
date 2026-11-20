from __future__ import annotations

import asyncio
from contextlib import ExitStack
from time import monotonic
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.engine import make_url

from src.core.errors import ExecutionOwnershipLost
from src.storage.postgres import (
    PostgreSQLStorage,
    bootstrap_postgres_database,
    create_postgres_engine,
    create_postgres_session_factory,
)
from tests.integrations.mcp import test_dispatch_claim as claim_tests
from tests.postgres_test_support import isolated_postgres_test_dsn_or_skip_reason
from tests.storage import test_mcp_dispatch_aggregate_repository as aggregate


class PostgresDispatchClaimTest(claim_tests.DispatchClaimTest):
    async def asyncSetUp(self):
        dsn, reason = isolated_postgres_test_dsn_or_skip_reason(
            "MAF_POSTGRES_MCP_LEASE_TEST_DSN"
        )
        if dsn is None:
            self.skipTest(reason)
        admin = create_postgres_engine(dsn)
        self.addCleanup(admin.dispose)
        schema = "mcp_lease_" + uuid4().hex
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))

        def drop_schema():
            with admin.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))

        self.addCleanup(drop_schema)
        isolated = make_url(dsn).update_query_dict(
            {"options": f"-csearch_path={schema}"}
        )
        engine = create_postgres_engine(
            isolated.render_as_string(hide_password=False),
            pool_size=2,
            max_overflow=0,
        )
        self.addCleanup(engine.dispose)
        with ExitStack() as stack:
            stack.enter_context(
                patch.object(aggregate, "create_sqlite_engine", lambda _: engine)
            )
            stack.enter_context(
                patch.object(
                    aggregate, "bootstrap_sqlite_database", bootstrap_postgres_database
                )
            )
            stack.enter_context(
                patch.object(
                    aggregate,
                    "create_sqlite_session_factory",
                    create_postgres_session_factory,
                )
            )
            stack.enter_context(
                patch.object(aggregate, "SQLiteStorage", PostgreSQLStorage)
            )
            await super().asyncSetUp()

    async def test_database_lock_wait_obeys_remaining_window_and_rolls_back(self):
        await self.coordinator._renew_dispatch_claim(self.authority)
        original_revision = self.authority.revision
        with self.fixture.engine.connect() as blocker:
            transaction = blocker.begin()
            blocker.execute(
                text(
                    "SELECT outbox_id FROM mcp_dispatch_resume_outbox "
                    "WHERE outbox_id=:id FOR UPDATE"
                ),
                {"id": self.authority.outbox_id},
            )
            try:
                self.authority.deadline = monotonic() + 0.15
                started = monotonic()
                with self.assertRaises(ExecutionOwnershipLost):
                    await self.coordinator._renew_dispatch_claim(self.authority)
                self.assertLess(monotonic() - started, 2)
            finally:
                transaction.rollback()
        await asyncio.sleep(0.05)
        current = await self.fixture.storage.get_mcp_dispatch_resume_outbox(
            self.fixture.outbox_id
        )
        self.assertEqual(current.revision, original_revision)
        # Transaction-local timeouts must not leak into the next borrower.
        with self.fixture.engine.connect() as connection:
            self.assertEqual(connection.scalar(text("SHOW statement_timeout")), "0")
            self.assertEqual(connection.scalar(text("SHOW lock_timeout")), "0")

    async def test_claim_bound_convergence_rejects_successor_in_same_transaction(self):
        # Use the existing claimed row, then transfer it under another connection.
        with self.fixture.engine.begin() as successor:
            successor.execute(
                text(
                    "UPDATE mcp_dispatch_resume_outbox SET claim_owner='new', "
                    "claim_token='new-token', revision=revision+1 WHERE outbox_id=:id"
                ),
                {"id": self.authority.outbox_id},
            )
        result = await self.fixture.storage.converge_claimed_mcp_unknown_no_replay(
            self.fixture.task.task_id,
            self.fixture.intent_id,
            self.fixture.outbox_id,
            "worker",
            "token",
            self.authority.revision,
            self.now,
        )
        self.assertIsNone(result)
        current = await self.fixture.storage.get_mcp_dispatch_resume_outbox(
            self.fixture.outbox_id
        )
        self.assertEqual(current.claim_token, "new-token")
        with self.assertRaises(ExecutionOwnershipLost):
            await self.coordinator._renew_dispatch_claim(self.authority)

    async def test_pool_wait_times_out_without_late_commit(self):
        original_revision = self.authority.revision
        with self.fixture.engine.connect(), self.fixture.engine.connect():
            self.authority.deadline = monotonic() + 0.05
            with self.assertRaises(ExecutionOwnershipLost):
                await asyncio.wait_for(
                    self.coordinator._renew_dispatch_claim(self.authority), 1
                )
            self.assertTrue(self.authority.lost)
        # The cancelled coroutine's thread can acquire a connection later. Its
        # expired operation context must reject SQL before it changes authority.
        await asyncio.sleep(0.05)
        current = await self.fixture.storage.get_mcp_dispatch_resume_outbox(
            self.fixture.outbox_id
        )
        self.assertEqual(current.revision, original_revision)
