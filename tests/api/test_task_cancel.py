from __future__ import annotations

import asyncio
import threading

from httpx_sse import aconnect_sse

from src.core.enums import TaskStatus
from src.core.models import Conversation, Task
from src.orchestration.conversation_memory import ConversationMemoryContext

from tests.api.support import APITestCase, blocking_mysql_adapter

PARTIAL_SENTINEL = "PARTIAL_SHOULD_NOT_PERSIST_7f3a"


class TaskCancelAPITest(APITestCase):
    async def test_submission_waits_for_memory_preparation_before_agent_handoff(self) -> None:
        class BlockingMemoryBuilder:
            def __init__(self) -> None:
                self.started = asyncio.Event()
                self.release = asyncio.Event()

            async def build(self, request, *, username=None):
                self.started.set()
                await self.release.wait()
                return ConversationMemoryContext(
                    conversation_id=request.conversation_id,
                    root_message_id=request.root_message_id,
                    source_message_count=1,
                    current_user_message=request.user_message,
                )

        builder = BlockingMemoryBuilder()
        await self.reconfigure_runtime(
            conversation_memory_builder=builder,
            skill_roots=[],
        )

        submission = asyncio.create_task(
            self.submit_message(
                conversation_id="conv-prepare-memory",
                content="在交接前完成记忆准备",
                capability_id=None,
            )
        )

        async def _memory_builder_started() -> bool:
            return builder.started.is_set()

        await self.wait_for_condition(_memory_builder_started)
        self.assertFalse(submission.done())
        tasks = await self.runtime.storage.list_tasks_for_conversation(
            "conv-prepare-memory"
        )
        self.assertEqual(len(tasks), 1)
        self.assertEqual(str(tasks[0].status), "accepted")
        self.assertIsNone(
            await self.runtime.agent_run_repository.get_run_for_task(
                tasks[0].task_id
            )
        )

        builder.release.set()
        response = await submission
        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]
        await self.runtime._await_existing_execution(task_id)
        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")

        events = await self.runtime.storage.list_events_for_task(task_id)
        event_types = [event.event_type for event in events]
        self.assertLess(
            event_types.index("conversation.memory_built"),
            event_types.index("task.graph_created"),
        )

    async def test_cancel_endpoint_drives_real_cancellation_and_audit_output(self) -> None:
        query_started = threading.Event()
        blocking_adapter, release = blocking_mysql_adapter(started=query_started)
        await self.reconfigure_runtime(mysql_adapter=blocking_adapter)

        response = await self.submit_message()
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]

        async def _query_started() -> bool:
            current = await self.runtime.storage.get_task(task_id)
            return (
                current is not None
                and current.status == "running"
                and query_started.is_set()
            )

        await self.wait_for_condition(_query_started)

        cancel_response = await self.client.post("/api/v1/tasks/cancel", json={"task_id": task_id})
        self.assertEqual(cancel_response.status_code, 202)
        self.assertTrue(cancel_response.json()["accepted"])

        async with aconnect_sse(self.client, "GET", f"/api/v1/tasks/{task_id}/events") as event_source:
            iterator = event_source.aiter_sse()
            seen = set()
            while "task.cancelled" not in seen:
                event = await asyncio.wait_for(iterator.__anext__(), timeout=2)
                seen.add(event.event)

        release.set()
        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "cancelled")
        self.assertTrue(terminal["cancel_requested"])

        audit_log = (self.workspace / "audit.jsonl").read_text(encoding="utf-8")
        self.assertIn("task.context_terminated", audit_log)
        self.assertIn("task.cancelled", audit_log)

    async def test_cancel_terminal_task_is_idempotent_and_does_not_rewrite_status(self) -> None:
        await self.runtime.storage.save_conversation(Conversation(conversation_id="conv-1", username="acc-1"))
        task = Task(
            task_id="task-terminal-api",
            conversation_id="conv-1",
            root_message_id="msg-terminal-api",
            status=TaskStatus.COMPLETED,
        )
        await self.runtime.storage.save_task(task)

        cancel_response = await self.client.post("/api/v1/tasks/cancel", json={"task_id": "task-terminal-api"})

        self.assertEqual(cancel_response.status_code, 202)
        self.assertEqual(cancel_response.json()["status"], "completed")
        reloaded = await self.runtime.storage.get_task("task-terminal-api")
        self.assertEqual(reloaded.status, TaskStatus.COMPLETED)
        self.assertIsNone(reloaded.cancel_requested_at)

    async def test_local_cancel_intent_does_not_rewrite_a_terminal_task(self) -> None:
        await self.runtime.storage.save_conversation(
            Conversation(conversation_id="conv-race", username="acc-1")
        )
        task = Task(
            task_id="task-terminal-race",
            conversation_id="conv-race",
            root_message_id="msg-terminal-race",
            status=TaskStatus.COMPLETED,
        )
        await self.runtime.storage.save_task(task)
        self.runtime._locally_cancelled_task_ids.add(task.task_id)

        restored = await self.runtime._restore_cancelled_task_if_requested(
            task.task_id,
            task.conversation_id,
        )

        self.assertIsNone(restored)
        reloaded = await self.runtime.storage.get_task(task.task_id)
        self.assertEqual(reloaded.status, TaskStatus.COMPLETED)
        events = await self.runtime.storage.list_events_for_task(task.task_id)
        self.assertFalse(any(event.event_type == "task.late_result_discarded" for event in events))

    async def test_cancel_stops_agent_sample_and_discards_late_answer(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def streamer(_prompt: str):
            started.set()
            await release.wait()
            return PARTIAL_SENTINEL

        await self.reconfigure_runtime(main_agent_stream_generator=streamer, skill_roots=None)
        response = await self.submit_message(
            conversation_id="conv-cancel-stream",
            content="生成一个长回答",
            capability_id=None,
        )
        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]

        await asyncio.wait_for(started.wait(), timeout=2)

        cancel_response = await self.client.post("/api/v1/tasks/cancel", json={"task_id": task_id})
        self.assertEqual(cancel_response.status_code, 202, cancel_response.text)
        release.set()
        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "cancelled")

        async def _execution_handle_removed() -> bool:
            return task_id not in self.runtime._running_tasks

        await self.wait_for_condition(_execution_handle_removed)

        events = await self.runtime.storage.list_events_for_task(task_id)
        self.assertFalse(any(event.event_type == "task.completed" for event in events))
        self.assertFalse(any(PARTIAL_SENTINEL in str(event.payload) for event in events))
        messages = await self.runtime.storage.list_messages_for_conversation("conv-cancel-stream")
        self.assertFalse(any(str(message.role) == "assistant" for message in messages))
