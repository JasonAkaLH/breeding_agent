from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from src.core.enums import EventVisibility, MessageRole, TaskStatus
from src.core.models import Conversation, EventRecord, Message, Task
from src.storage.rust_contract import resource_limit
from tests.api.support import APITestCase, blocking_mysql_adapter


class TaskEventsSSEAPITest(APITestCase):
    async def test_task_events_replay_reads_history_by_rust_limited_pages(self) -> None:
        page_limit = resource_limit("replay_page_events")
        created_at = datetime(2026, 5, 15, 12, 0, 0)
        await self.runtime.storage.save_conversation(
            Conversation(conversation_id="conv-page", account_id="account-page", created_at=created_at, updated_at=created_at)
        )
        await self.runtime.storage.save_message(
            Message(
                message_id="msg-page",
                conversation_id="conv-page",
                role=MessageRole.USER,
                content="分页 replay",
                task_id="task-page",
                created_at=created_at,
            )
        )
        await self.runtime.storage.save_task(
            Task(
                task_id="task-page",
                conversation_id="conv-page",
                root_message_id="msg-page",
                status=TaskStatus.COMPLETED,
                created_at=created_at,
                updated_at=created_at,
            )
        )
        for index in range(page_limit + 1):
            await self.runtime.storage.append_event(
                EventRecord(
                    event_id=f"evt-api-page-{index:04d}",
                    conversation_id="conv-page",
                    task_id="task-page",
                    event_type="task.completed" if index == page_limit else "task.progress",
                    payload={"index": index},
                    visibility=EventVisibility.FRONTEND,
                    created_at=created_at + timedelta(microseconds=index),
                )
            )

        events = [event async for event in self.runtime.iter_frontend_events("task-page")]

        self.assertEqual(len(events), page_limit + 1)
        self.assertEqual(events[-1].event_type, "task.completed")
        self.assertEqual(events[-1].event_id, f"evt-api-page-{page_limit:04d}")

    async def test_task_events_endpoint_replays_history_and_streams_live_completion(self) -> None:
        blocking_adapter, release = blocking_mysql_adapter()
        await self.reconfigure_runtime(mysql_adapter=blocking_adapter)

        response = await self.submit_message()
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]

        iterator = self.runtime.iter_frontend_events(task_id).__aiter__()
        first = await asyncio.wait_for(iterator.__anext__(), timeout=2)
        second = await asyncio.wait_for(iterator.__anext__(), timeout=2)
        replay_types = {first.event_type, second.event_type}
        self.assertIn("task.accepted", replay_types)
        self.assertIn("task.graph_created", replay_types)

        release.set()

        seen_types = set(replay_types)
        while "task.completed" not in seen_types:
            event = await asyncio.wait_for(iterator.__anext__(), timeout=2)
            seen_types.add(event.event_type)

        self.assertIn("node.started", seen_types)
        self.assertIn("task.completed", seen_types)
