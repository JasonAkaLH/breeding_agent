from __future__ import annotations

import asyncio
import unittest

from src.api.runtime import ApiRuntime
from src.core.enums import TaskStatus
from src.core.models import Conversation, Task
from src.orchestration.agent_loop.models import AgentStorageConflict
from src.orchestration.agent_loop.orchestrator import AgentExecutionRequest


class ExecutionSingleflightTest(unittest.IsolatedAsyncioTestCase):
    def _runtime(self) -> ApiRuntime:
        runtime = object.__new__(ApiRuntime)
        runtime._lock = asyncio.Lock()
        runtime._running_tasks = {}
        runtime._execution_generations = {}
        runtime._execution_durable_starts = {}
        runtime._execution_wait_timeout_seconds = 0.01
        runtime._retain_task_skill_revision = lambda request: None
        runtime._retain_task_mcp_revision = lambda request: None
        return runtime

    async def test_schedule_can_wait_only_until_durable_agent_run_initialization(self) -> None:
        runtime = self._runtime()
        initialized = asyncio.Event()
        release = asyncio.Event()
        run_is_durable = False

        async def run(
            request,
            *,
            active_task_count,
            execution_generation=None,
            durable_start=None,
        ):
            nonlocal run_is_durable
            initialized.set()
            await release.wait()
            run_is_durable = True
            durable_start.set_result(None)

        class Runs:
            async def get_run_for_task(self, _task_id):
                return object() if run_is_durable else None

        runtime._run_execution = run
        runtime.agent_run_repository = Runs()
        scheduled = asyncio.create_task(
            runtime._schedule_execution(
                self._request(),
                await_durable_start=True,
            )
        )
        await initialized.wait()
        self.assertFalse(scheduled.done())
        release.set()
        handle = await scheduled
        await handle

    async def test_durable_start_failure_propagates_to_scheduler(self) -> None:
        runtime = self._runtime()

        async def run(
            request,
            *,
            active_task_count,
            execution_generation=None,
            durable_start=None,
        ):
            durable_start.set_exception(RuntimeError("durable_init_failed"))

        class Runs:
            async def get_run_for_task(self, _task_id):
                return None

        runtime._run_execution = run
        runtime.agent_run_repository = Runs()
        with self.assertRaisesRegex(RuntimeError, "durable_init_failed"):
            await runtime._schedule_execution(
                self._request(),
                await_durable_start=True,
            )

    @staticmethod
    def _request() -> AgentExecutionRequest:
        return AgentExecutionRequest(
            task_id="task-1",
            conversation_id="conversation-1",
            root_message_id="message-1",
            user_message="run",
            owner_scope="owner:test",
        )

    async def test_schedule_returns_existing_live_generation(self) -> None:
        runtime = self._runtime()
        release = asyncio.Event()
        calls = 0

        async def run(request, *, active_task_count, execution_generation=None):
            nonlocal calls
            calls += 1
            await release.wait()

        runtime._run_execution = run

        first = await runtime._schedule_execution(self._request())
        second = await runtime._schedule_execution(self._request())

        self.assertIs(first, second)
        await asyncio.sleep(0)
        self.assertEqual(calls, 1)
        self.assertEqual(runtime._execution_generations, {"task-1": 1})
        release.set()
        await first

    async def test_completed_generation_can_be_replaced_without_old_cleanup(self) -> None:
        runtime = self._runtime()

        async def run(request, *, active_task_count, execution_generation=None):
            return None

        runtime._run_execution = run
        first = await runtime._schedule_execution(self._request())
        await first
        second = await runtime._schedule_execution(self._request())

        self.assertIsNot(first, second)
        self.assertIs(runtime._running_tasks["task-1"], second)
        self.assertEqual(runtime._execution_generations["task-1"], 2)
        await second

    async def test_wait_timeout_keeps_live_handle_for_claim_recovery(self) -> None:
        runtime = self._runtime()
        release = asyncio.Event()
        handle = asyncio.create_task(release.wait())
        runtime._running_tasks["task-1"] = handle

        await runtime._await_existing_execution("task-1")

        self.assertFalse(handle.done())
        self.assertIs(runtime._running_tasks["task-1"], handle)
        release.set()
        await handle

    async def test_second_runtime_lease_holder_exits_without_failing_live_owner(self) -> None:
        request = self._request()
        conversation = Conversation(
            conversation_id=request.conversation_id,
            username="alice",
            current_task_id=request.task_id,
        )
        task = Task(
            task_id=request.task_id,
            conversation_id=request.conversation_id,
            root_message_id=request.root_message_id,
            status=TaskStatus.RUNNING,
        )

        class SharedStorage:
            async def get_conversation(self, conversation_id):
                self.assert_identity(conversation_id, conversation.conversation_id)
                return conversation

            async def get_task(self, task_id):
                self.assert_identity(task_id, task.task_id)
                return task

            async def save_conversation(self, _conversation):
                raise AssertionError("live duplicate must not clear the task pointer")

            @staticmethod
            def assert_identity(actual, expected):
                if actual != expected:
                    raise AssertionError(f"unexpected identity: {actual}")

        class LeaseContendedOrchestrator:
            def __init__(self) -> None:
                self.first_started = asyncio.Event()
                self.release_first = asyncio.Event()
                self.start_calls = 0
                self.capability_calls = 0

            async def start_or_resume(self, _request, *, cancellation=None):
                self.start_calls += 1
                if self.start_calls == 1:
                    self.capability_calls += 1
                    self.first_started.set()
                    await self.release_first.wait()
                    return None
                raise AgentStorageConflict("agent_task_lease_held")

        shared_storage = SharedStorage()
        orchestrator = LeaseContendedOrchestrator()
        failed_events: list[Exception] = []

        def configured_runtime() -> ApiRuntime:
            runtime = self._runtime()
            runtime.storage = shared_storage
            runtime.agent_loop_orchestrator = orchestrator
            runtime.user_mcp_gateway = None
            runtime._locally_cancelled_task_ids = set()
            runtime._agent_cancellation_token = lambda _task_id: None

            async def identity_execution_request(value):
                return value

            async def no_cancel_restore(_task_id, _conversation_id):
                return None

            async def record_failure(_request, exc):
                failed_events.append(exc)

            async def no_release(_task_id):
                return None

            runtime._scrub_deleted_file_context_for_execution = identity_execution_request
            runtime._attach_conversation_memory = identity_execution_request
            runtime._restore_cancelled_task_if_requested = no_cancel_restore
            runtime._mark_task_failed = record_failure
            runtime._release_task_skill_revision_if_terminal = no_release
            runtime._release_task_mcp_revision_if_terminal = no_release
            return runtime

        first_runtime = configured_runtime()
        second_runtime = configured_runtime()
        first_execution = asyncio.create_task(
            first_runtime._run_execution(request, active_task_count=0)
        )
        await orchestrator.first_started.wait()

        await second_runtime._run_execution(request, active_task_count=0)

        self.assertEqual(failed_events, [])
        self.assertEqual(task.status, TaskStatus.RUNNING)
        self.assertEqual(conversation.current_task_id, request.task_id)
        self.assertEqual(orchestrator.capability_calls, 1)
        orchestrator.release_first.set()
        await first_execution


if __name__ == "__main__":
    unittest.main()
