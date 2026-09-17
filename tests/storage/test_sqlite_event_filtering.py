from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

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
