from __future__ import annotations

import asyncio
from types import SimpleNamespace

from datetime import datetime, timedelta


from src.api.routes.tasks import _iter_authorized_frontend_events
from src.api.sse import InMemoryEventBroker
from src.core.enums import EventVisibility, MessageRole, TaskStatus
from src.core.models import Conversation, EventRecord, Message, Task
from src.storage.rust_contract import resource_limit
from tests.api.support import APITestCase, blocking_mysql_adapter


class _FailingAuditSink:
    async def record(self, *_args, **_kwargs) -> None:
        raise RuntimeError("audit sink unavailable")


class TaskEventsSSEAPITest(APITestCase):
    async def test_task_events_replay_reads_history_by_rust_limited_pages(self) -> None:
        page_limit = resource_limit("replay_page_events")
        created_at = datetime(2026, 5, 15, 12, 0, 0)
        await self.runtime.storage.save_conversation(
            Conversation(conversation_id="conv-page", username="account-page", created_at=created_at, updated_at=created_at)
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

    async def test_task_events_endpoint_accepts_authorization_token(self) -> None:
        await self.logout()
        login = await self.client.post("/api/v1/auth/login", json={"username": "alice"})
        self.assertEqual(login.status_code, 200, login.text)
        access_token = login.json()["access_token"]
        await self.runtime.storage.save_conversation(
            Conversation(conversation_id="conv-bearer-sse", username="alice")
        )
        await self.runtime.storage.save_task(
            Task(
                task_id="task-bearer-sse",
                conversation_id="conv-bearer-sse",
                root_message_id="msg-bearer-sse",
                status=TaskStatus.COMPLETED,
            )
        )
        await self.runtime.storage.append_event(
            EventRecord(
                event_id="evt-bearer-sse",
                conversation_id="conv-bearer-sse",
                task_id="task-bearer-sse",
                event_type="agent.run.completed",
                payload={
                    "compaction_count": 0,
                    "duration_seconds": 0,
                    "outcome": "completed",
                    "sample_count": 1,
                    "tool_call_count": 0,
                },
                visibility=EventVisibility.FRONTEND,
            )
        )
        self.client.cookies.clear()

        response = await self.client.get(
            "/api/v1/tasks/task-bearer-sse/events",
            headers={"Authorization": f"Bearer {access_token}", "Accept": "text/event-stream"},
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("text/event-stream", response.headers.get("content-type", ""))
        self.assertIn("evt-bearer-sse", response.text)
        self.assertIn("agent.run.completed", response.text)

    async def test_task_events_live_subscription_covers_replay_to_live_gap(self) -> None:
        created_at = datetime(2026, 5, 15, 12, 0, 0)
        await self.runtime.storage.save_conversation(
            Conversation(conversation_id="conv-gap", username="account-gap", created_at=created_at, updated_at=created_at)
        )
        await self.runtime.storage.save_task(
            Task(
                task_id="task-gap",
                conversation_id="conv-gap",
                root_message_id="msg-gap",
                status=TaskStatus.RUNNING,
                created_at=created_at,
                updated_at=created_at,
            )
        )
        replay_event = EventRecord(
            event_id="evt-gap-replay",
            conversation_id="conv-gap",
            task_id="task-gap",
            event_type="task.accepted",
            payload={},
            visibility=EventVisibility.FRONTEND,
            created_at=created_at,
        )
        live_gap_event = EventRecord(
            event_id="evt-gap-live",
            conversation_id="conv-gap",
            task_id="task-gap",
            event_type="node.started",
            payload={"capability_id": "skill.example"},
            visibility=EventVisibility.FRONTEND,
            created_at=created_at + timedelta(microseconds=1),
        )

        async def replay_then_publish_gap(task_id: str):
            self.assertEqual(task_id, "task-gap")
            yield replay_event
            await self.runtime._record_event(live_gap_event)

        self.runtime._iter_event_replay_pages = replay_then_publish_gap  # type: ignore[method-assign]

        iterator = self.runtime.iter_frontend_events("task-gap").__aiter__()
        self.assertEqual((await asyncio.wait_for(iterator.__anext__(), timeout=2)).event_id, "evt-gap-replay")
        self.assertEqual((await asyncio.wait_for(iterator.__anext__(), timeout=2)).event_id, "evt-gap-live")
        aclose = getattr(iterator, "aclose", None)
        if callable(aclose):
            await aclose()

    async def test_event_broker_fanout_survives_audit_sink_failure(self) -> None:
        broker = InMemoryEventBroker(audit_sink=_FailingAuditSink())
        subscription = broker.subscribe("task-audit-fanout")
        event = EventRecord(
            event_id="evt-audit-fanout",
            conversation_id="conv-audit-fanout",
            task_id="task-audit-fanout",
            event_type="node.waiting_for_input",
            payload={"interrupt_id": "interrupt-1"},
            visibility=EventVisibility.FRONTEND,
            created_at=datetime(2026, 5, 15, 12, 0, 0),
        )

        with self.assertLogs("src.api.sse", level="WARNING") as logs:
            await broker.publish(event)

        received = await asyncio.wait_for(subscription.get(), timeout=1)
        self.assertEqual(received.event_id, "evt-audit-fanout")
        self.assertTrue(any("event_broker_audit_sink_failed" in item for item in logs.output))
        subscription.close()


    async def test_open_task_event_stream_stops_after_token_refresh(self) -> None:
        await self.logout()
        login = await self.client.post("/api/v1/auth/login", json={"username": "alice"})
        self.assertEqual(login.status_code, 200, login.text)
        old_token = login.json()["access_token"]
        created_at = datetime(2026, 5, 25, 13, 0, 0)
        await self.runtime.storage.save_conversation(
            Conversation(conversation_id="conv-refresh-sse", username="alice", created_at=created_at, updated_at=created_at)
        )
        await self.runtime.storage.save_task(
            Task(
                task_id="task-refresh-sse",
                conversation_id="conv-refresh-sse",
                root_message_id="msg-refresh-sse",
                status=TaskStatus.RUNNING,
                created_at=created_at,
                updated_at=created_at,
            )
        )
        request = SimpleNamespace(
            headers={"Authorization": f"Bearer {old_token}"},
            app=SimpleNamespace(state=SimpleNamespace(runtime=self.runtime)),
        )
        iterator = _iter_authorized_frontend_events(
            self.runtime,
            "task-refresh-sse",
            request,
            "alice",
        ).__aiter__()
        pending = asyncio.create_task(iterator.__anext__())
        await asyncio.sleep(0.05)

        refreshed = await self.client.post(
            "/api/v1/auth/refresh-token",
            headers={"Authorization": f"Bearer {old_token}"},
        )
        self.assertEqual(refreshed.status_code, 200, refreshed.text)

        await self.runtime._record_event(
            EventRecord(
                event_id="evt-refresh-sse",
                conversation_id="conv-refresh-sse",
                task_id="task-refresh-sse",
                event_type="task.completed",
                payload={},
                visibility=EventVisibility.FRONTEND,
                created_at=created_at,
            )
        )

        event = await asyncio.wait_for(pending, timeout=2)
        self.assertEqual(event.event_type, "auth.invalidated")
        self.assertEqual(event.payload["reason"], "auth_generation_mismatch")

    async def test_idle_task_event_stream_revalidates_current_token_without_new_events(self) -> None:
        await self.logout()
        login = await self.client.post("/api/v1/auth/login", json={"username": "alice"})
        self.assertEqual(login.status_code, 200, login.text)
        old_token = login.json()["access_token"]
        await self.runtime.storage.save_conversation(
            Conversation(conversation_id="conv-idle-sse", username="alice")
        )
        await self.runtime.storage.save_task(
            Task(
                task_id="task-idle-sse",
                conversation_id="conv-idle-sse",
                root_message_id="msg-idle-sse",
                status=TaskStatus.RUNNING,
            )
        )
        request = SimpleNamespace(
            headers={"Authorization": f"Bearer {old_token}"},
            app=SimpleNamespace(state=SimpleNamespace(runtime=self.runtime)),
        )
        iterator = _iter_authorized_frontend_events(
            self.runtime,
            "task-idle-sse",
            request,
            "alice",
            revalidation_interval_seconds=0.05,
        ).__aiter__()
        pending = asyncio.create_task(iterator.__anext__())
        await asyncio.sleep(0.08)

        refreshed = await self.client.post(
            "/api/v1/auth/refresh-token",
            headers={"Authorization": f"Bearer {old_token}"},
        )
        self.assertEqual(refreshed.status_code, 200, refreshed.text)

        event = await asyncio.wait_for(pending, timeout=1)
        self.assertEqual(event.event_type, "auth.invalidated")
        self.assertEqual(event.payload["reason"], "auth_generation_mismatch")

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
        while "agent.run.completed" not in seen_types:
            event = await asyncio.wait_for(iterator.__anext__(), timeout=2)
            seen_types.add(event.event_type)

        self.assertIn("node.started", seen_types)
        self.assertIn("agent.run.completed", seen_types)
