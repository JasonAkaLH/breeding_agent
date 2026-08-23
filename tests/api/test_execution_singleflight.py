from __future__ import annotations

import asyncio
import unittest

from src.api.runtime import ApiRuntime
from src.orchestration.agent_loop.orchestrator import AgentExecutionRequest


class ExecutionSingleflightTest(unittest.IsolatedAsyncioTestCase):
    def _runtime(self) -> ApiRuntime:
        runtime = object.__new__(ApiRuntime)
        runtime._lock = asyncio.Lock()
        runtime._running_tasks = {}
        runtime._execution_generations = {}
        runtime._execution_wait_timeout_seconds = 0.01
        runtime._retain_task_skill_revision = lambda request: None
        runtime._retain_task_mcp_revision = lambda request: None
        return runtime

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


if __name__ == "__main__":
    unittest.main()
