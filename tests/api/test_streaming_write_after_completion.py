from __future__ import annotations

import asyncio
import contextlib
import json
import unittest
from types import SimpleNamespace

from src.api.routes.tasks import _iter_authorized_frontend_events
from src.api.sse import InMemoryEventBroker
from src.core.enums import EventVisibility, TaskStatus
from src.core.models import Conversation, EventRecord, Task
from tests.api.support import APITestCase


PARTIAL_SENTINEL = "PARTIAL_SHOULD_NOT_PERSIST_7f3a"


class _CapturingAuditSink:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict]] = []

    async def record(self, event_type: str, payload: dict, **_kwargs) -> None:
        self.records.append((event_type, dict(payload)))


class StreamingWriteAfterCompletionUnitTest(unittest.IsolatedAsyncioTestCase):
    async def test_transient_publish_bypasses_storage_and_full_audit(self) -> None:
        audit_sink = _CapturingAuditSink()
        broker = InMemoryEventBroker(audit_sink=audit_sink)
        subscription = broker.subscribe("task-transient")
        event = EventRecord(
            event_id="evt-transient",
            conversation_id="conv-transient",
            task_id="task-transient",
            event_type="main_agent.output_delta",
            payload={"delta": PARTIAL_SENTINEL, "ordinal": 1},
            visibility=EventVisibility.FRONTEND,
        )

        await broker.publish_transient(event)

        received = await asyncio.wait_for(subscription.get(), timeout=1)
        self.assertEqual(received.payload["delta"], PARTIAL_SENTINEL)
        self.assertEqual(audit_sink.records, [])

        await broker.publish(event)
        self.assertIn(PARTIAL_SENTINEL, json.dumps(audit_sink.records, ensure_ascii=False))


class StreamingWriteAfterCompletionAPITest(APITestCase):
    async def test_stream_failure_discards_partial_output_and_records_redacted_diagnostic(self) -> None:
        async def streamer(_prompt: str):
            yield PARTIAL_SENTINEL
            raise TimeoutError("provider timed out after partial output")

        await self.reconfigure_runtime(main_agent_stream_generator=streamer, skill_roots=None)
        response = await self.submit_message(
            conversation_id="conv-stream-fail",
            content="触发失败",
            capability_id=None,
        )
        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]

        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "failed")

        events = await self.runtime.storage.list_events_for_task(task_id)
        serialized_events = json.dumps([dict(event.payload) for event in events], ensure_ascii=False, default=str)
        self.assertNotIn(PARTIAL_SENTINEL, serialized_events)
        failure_event = next(event for event in events if event.event_type == "main_agent.llm_stream_failed")
        self.assertTrue(failure_event.payload["partial_output_discarded"])
        self.assertEqual(failure_event.payload["answer_chunk_count"], 1)
        self.assertEqual(failure_event.payload["answer_char_count"], len(PARTIAL_SENTINEL))
        self.assertEqual(failure_event.payload["error_type"], "TimeoutError")
        self.assertFalse(any(event.event_type == "main_agent.output_final" for event in events))

        messages = await self.runtime.storage.list_messages_for_conversation("conv-stream-fail")
        artifacts = await self.runtime.storage.list_artifacts_for_task(task_id)
        self.assertFalse(any(str(message.role) == "assistant" for message in messages))
        self.assertFalse(any(PARTIAL_SENTINEL in artifact.storage_ref for artifact in artifacts))
        self.assertNotIn(PARTIAL_SENTINEL, (self.workspace / "audit.jsonl").read_text(encoding="utf-8"))

    async def test_sse_revalidation_uses_readonly_token_check_without_touch_per_event(self) -> None:
        await self.logout()
        login = await self.client.post("/api/v1/auth/login", json={"username": "alice"})
        self.assertEqual(login.status_code, 200, login.text)
        token = login.json()["access_token"]
        await self.runtime.storage.save_conversation(Conversation(conversation_id="conv-readonly-sse", username="alice"))
        await self.runtime.storage.save_task(
            Task(
                task_id="task-readonly-sse",
                conversation_id="conv-readonly-sse",
                root_message_id="msg-readonly-sse",
                status=TaskStatus.RUNNING,
            )
        )

        touch_count = 0
        original_touch = self.runtime.storage.touch_auth_user_token_last_used

        async def counting_touch(*args, **kwargs):
            nonlocal touch_count
            touch_count += 1
            return await original_touch(*args, **kwargs)

        self.runtime.storage.touch_auth_user_token_last_used = counting_touch  # type: ignore[method-assign]
        request = SimpleNamespace(
            headers={"Authorization": f"Bearer {token}"},
            app=SimpleNamespace(state=SimpleNamespace(runtime=self.runtime)),
        )
        iterator = _iter_authorized_frontend_events(
            self.runtime,
            "task-readonly-sse",
            request,
            "alice",
            revalidation_interval_seconds=10,
        ).__aiter__()
        pending = asyncio.create_task(iterator.__anext__())
        await asyncio.sleep(0.05)

        for index in range(10):
            await self.runtime._publish_transient_event(
                EventRecord(
                    event_id=f"evt-readonly-sse-{index}",
                    conversation_id="conv-readonly-sse",
                    task_id="task-readonly-sse",
                    event_type="main_agent.output_delta",
                    payload={"delta": f"chunk-{index}", "ordinal": index + 1},
                    visibility=EventVisibility.FRONTEND,
                )
            )
            event = await asyncio.wait_for(pending, timeout=1)
            self.assertEqual(event.event_type, "main_agent.output_delta")
            pending = asyncio.create_task(iterator.__anext__())

        pending.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pending
        aclose = getattr(iterator, "aclose", None)
        if callable(aclose):
            with contextlib.suppress(RuntimeError):
                await aclose()
        self.assertEqual(touch_count, 0)
