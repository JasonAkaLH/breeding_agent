from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from unittest.mock import patch

from src.core.enums import EventVisibility
from src.core.models import EventRecord
from src.storage.rust_contract import resource_limit
from src.storage.sqlite.repositories import SQLiteStorage
from tests.storage.support import SQLiteStorageTestCase


class SQLiteEventFilteringTest(SQLiteStorageTestCase):
    def _save_event(
        self,
        storage: SQLiteStorage,
        *,
        event_id: str,
        task_id: str = "task-filter",
        node_id: str | None = "node-final",
        event_type: str = "agent.final_output",
        visibility: EventVisibility = EventVisibility.FRONTEND,
        created_at: datetime | None = None,
        payload: dict | None = None,
    ) -> None:
        asyncio.run(
            storage.append_event(
                EventRecord(
                    event_id=event_id,
                    conversation_id="conv-filter",
                    task_id=task_id,
                    node_id=node_id,
                    event_type=event_type,
                    visibility=visibility,
                    payload=payload or {"response_role": "final"},
                    created_at=created_at,
                )
            )
        )

    def test_filtered_event_read_is_db_side_and_survives_unmatched_replay_volume(self) -> None:
        storage = SQLiteStorage(self.session_factory)
        page_limit = resource_limit("replay_page_events")
        for index in range(page_limit + 1):
            self._save_event(
                storage,
                event_id=f"evt-reasoning-{index:04d}",
                node_id="node-final",
                event_type="agent.reasoning_delta",
                payload={"delta": f"reasoning-{index}"},
                created_at=datetime(2026, 5, 26, 8, 0, 0) + timedelta(seconds=index),
            )
        self._save_event(
            storage,
            event_id="evt-final",
            node_id="node-final",
            event_type="agent.final_output",
            payload={"response_role": "final"},
            created_at=datetime(2026, 5, 26, 9, 0, 0),
        )

        with self.assertRaisesRegex(ValueError, "event_log_replay_page_exceeded"):
            asyncio.run(storage.list_events_for_task("task-filter"))

        filtered = asyncio.run(
            storage.list_events_for_task_filtered(
                "task-filter",
                event_types={"agent.final_output"},
                visibility=EventVisibility.FRONTEND,
                limit=32,
            )
        )

        self.assertEqual([event.event_id for event in filtered], ["evt-final"])
        self.assertEqual(filtered[0].payload["response_role"], "final")

    def test_filtered_event_read_filters_by_type_visibility_node_and_orders_deterministically(self) -> None:
        storage = SQLiteStorage(self.session_factory)
        base = datetime(2026, 5, 26, 8, 0, 0)
        self._save_event(storage, event_id="evt-final-b", node_id="node-final", created_at=base + timedelta(seconds=2))
        self._save_event(storage, event_id="evt-other-node", node_id="node-other", created_at=base + timedelta(seconds=1))
        self._save_event(
            storage,
            event_id="evt-internal",
            node_id="node-final",
            visibility=EventVisibility.INTERNAL,
            created_at=base + timedelta(seconds=1),
        )
        self._save_event(
            storage,
            event_id="evt-delta",
            node_id="node-final",
            event_type="agent.reasoning_delta",
            created_at=base + timedelta(seconds=1),
        )
        self._save_event(storage, event_id="evt-final-a", node_id="node-final", created_at=base + timedelta(seconds=1))

        filtered = asyncio.run(
            storage.list_events_for_task_filtered(
                "task-filter",
                event_types={"agent.final_output"},
                visibility=EventVisibility.FRONTEND,
                node_id="node-final",
                limit=10,
            )
        )

        self.assertEqual([event.event_id for event in filtered], ["evt-final-a", "evt-final-b"])

    def test_filtered_event_read_rejects_over_contract_limit(self) -> None:
        storage = SQLiteStorage(self.session_factory)
        with self.assertRaisesRegex(ValueError, "event_log_replay_page_exceeded"):
            asyncio.run(
                storage.list_events_for_task_filtered(
                    "task-filter",
                    event_types={"agent.final_output"},
                    limit=resource_limit("replay_page_events") + 1,
                )
            )

    def test_event_pages_resume_at_the_byte_boundary_with_null_and_equal_timestamps(self) -> None:
        storage = SQLiteStorage(self.session_factory)
        base = datetime(2026, 11, 23, 12, 0, 0)
        payload_limit = resource_limit("event_payload_bytes")
        payload = {"text": "点" * ((payload_limit - 32) // 3)}
        for index in reversed(range(6)):
            self._save_event(
                storage,
                event_id=f"evt-{index}",
                created_at=None if index < 2 else base,
                payload=payload,
            )

        async def read_pages():
            first = await storage.list_event_page_for_task("task-filter")
            second = await storage.list_event_page_for_task(
                "task-filter", after_event_id=first[-1].event_id
            )
            end = await storage.list_event_page_for_task(
                "task-filter", after_event_id=second[-1].event_id
            )
            return [[event.event_id for event in page] for page in (first, second, end)]

        self.assertEqual(
            asyncio.run(read_pages()),
            [["evt-0", "evt-1", "evt-2", "evt-3"], ["evt-4", "evt-5"], []],
        )

    def test_event_pages_cover_row_count_boundaries_in_order(self) -> None:
        storage = SQLiteStorage(self.session_factory)
        limit = resource_limit("replay_page_events")
        created_at = datetime(2026, 11, 23, 12, 0, 0)

        async def check_boundaries():
            event_ids: list[str] = []
            for count in (0, 1, limit - 1, limit, limit + 1, limit + 7, 2 * limit + 1):
                with self.subTest(count=count):
                    for index in range(len(event_ids), count):
                        event_id = f"evt-{index:06d}"
                        await storage.append_event(EventRecord(
                            event_id=event_id,
                            conversation_id="conv-counts",
                            task_id="task-counts",
                            event_type="task.progress",
                            created_at=created_at,
                        ))
                        event_ids.append(event_id)

                    cursor = None
                    for offset in range(0, count, limit):
                        page = await storage.list_event_page_for_task("task-counts", after_event_id=cursor)
                        self.assertEqual([event.event_id for event in page], event_ids[offset:offset + limit])
                        cursor = page[-1].event_id
                    self.assertEqual(
                        await storage.list_event_page_for_task("task-counts", after_event_id=cursor), []
                    )

        asyncio.run(check_boundaries())

    def test_event_page_rejects_a_single_event_that_cannot_fit(self) -> None:
        storage = SQLiteStorage(self.session_factory)
        self._save_event(storage, event_id="evt-a", payload={"text": "fits"})
        self._save_event(storage, event_id="evt-b", payload={"text": "x" * 128})
        with patch(
            "src.storage.sqlite.repositories._ensure_event_replay_policy_compatible_with_rust_contract",
            return_value=(1000, 32),
        ):
            first = asyncio.run(storage.list_event_page_for_task("task-filter"))
            self.assertEqual([event.event_id for event in first], ["evt-a"])
            with self.assertRaisesRegex(ValueError, "event_log_replay_page_exceeded"):
                asyncio.run(storage.list_event_page_for_task("task-filter", after_event_id="evt-a"))
