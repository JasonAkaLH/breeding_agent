from __future__ import annotations

import unittest
from datetime import datetime
from uuid import uuid4

from src.core.models import EventRecord
from src.storage.postgres import (
    bootstrap_postgres_database,
    create_postgres_engine,
    create_postgres_session_factory,
)
from src.storage.postgres.repositories import PostgreSQLStorage
from src.storage.rust_contract import resource_limit
from tests.postgres_test_support import isolated_postgres_test_dsn_or_skip_reason


class EventPaginationPostgresTest(unittest.IsolatedAsyncioTestCase):
    async def test_event_pages_follow_postgres_order_across_byte_and_null_boundaries(self) -> None:
        dsn, reason = isolated_postgres_test_dsn_or_skip_reason(
            "MAF_POSTGRES_EVENT_PAGINATION_TEST_DSN"
        )
        if dsn is None:
            self.skipTest(reason)
        engine = create_postgres_engine(dsn)
        self.addCleanup(engine.dispose)
        bootstrap_postgres_database(engine)
        storage = PostgreSQLStorage(create_postgres_session_factory(engine))
        task_id = f"pagination-{uuid4().hex}"
        base = datetime(2026, 11, 23, 12, 0, 0)
        payload = {"text": "点" * ((resource_limit("event_payload_bytes") - 32) // 3)}
        event_ids = [f"{task_id}-{index}" for index in range(6)]
        for index in reversed(range(6)):
            await storage.append_event(
                EventRecord(
                    event_id=event_ids[index],
                    conversation_id=task_id,
                    task_id=task_id,
                    event_type="task.progress",
                    created_at=None if index < 2 else base,
                    payload=payload,
                )
            )

        first = await storage.list_event_page_for_task(task_id)
        self.assertEqual([event.event_id for event in first], event_ids[2:])
        cursor = first[-1].event_id
        for expected_id in event_ids[:2]:
            page = await storage.list_event_page_for_task(task_id, after_event_id=cursor, limit=1)
            self.assertEqual([event.event_id for event in page], [expected_id])
            cursor = page[-1].event_id
        self.assertEqual(await storage.list_event_page_for_task(task_id, after_event_id=cursor), [])
