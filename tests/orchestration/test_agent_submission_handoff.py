from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.core.enums import EventVisibility, TaskStatus
from src.core.models import EventRecord
from src.orchestration.agent_loop.capability_invoker import (
    AgentInvocationContextStore,
)
from src.orchestration.agent_loop.context_budget import AgentContextBudget
from src.orchestration.agent_loop.models import (
    AgentModelBinding,
    AgentRun,
    AgentRunStatus,
    AgentStorageConflict,
)
from src.orchestration.agent_loop.orchestrator import (
    AgentExecutionRequest,
    AgentLoopOrchestrator,
)
from src.orchestration.agent_loop.runner import AgentLoopRunResult
from src.storage.sqlite import (
    SQLiteAgentRepository,
    SQLiteStorage,
    bootstrap_sqlite_database,
    create_sqlite_engine,
    create_sqlite_session_factory,
)
from src.storage.sqlite.models import TaskRow


class _ExactEventStore:
    def __init__(self) -> None:
        self.events: dict[str, EventRecord] = {}
        self.fail_once_for_type: str | None = None
        self.load_calls: list[tuple[str, str]] = []
        self.record_calls: list[str] = []

    async def load(self, task_id: str, event_id: str) -> EventRecord | None:
        self.load_calls.append((task_id, event_id))
        event = self.events.get(event_id)
        if event is None or event.task_id != task_id:
            return None
        return event

    async def record(self, event: EventRecord) -> None:
        if self.fail_once_for_type == event.event_type:
            self.fail_once_for_type = None
            raise RuntimeError("injected_event_write_failure")
        existing = self.events.get(event.event_id)
        if existing is not None and existing != event:
            raise AgentStorageConflict("event_identity_conflict")
        self.events[event.event_id] = event
        self.record_calls.append(event.event_id)


class _WaitingRunner:
    def __init__(self, runs) -> None:
        self._runs = runs
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def run(self, run_id: str, **kwargs) -> AgentLoopRunResult:
        self.calls.append((run_id, dict(kwargs)))
        run = await self._runs.get_run(run_id)
        assert run is not None
        return AgentLoopRunResult(
            run=run,
            state="waiting",
            lease_handle=None,  # type: ignore[arg-type]
        )


class _UnusedFinalOutput:
    async def publish(self, **_kwargs):
        raise AssertionError("final output must not be published in this suite")


class _CreateRaceRepository:
    def __init__(self, repository) -> None:
        self._repository = repository
        self._initial_reads = 0
        self._initial_reads_lock = asyncio.Lock()
        self._both_read = asyncio.Event()

    def __getattr__(self, name: str):
        return getattr(self._repository, name)

    async def get_run_for_task(self, task_id: str):
        async with self._initial_reads_lock:
            self._initial_reads += 1
            read_number = self._initial_reads
            if self._initial_reads == 2:
                self._both_read.set()
        if read_number <= 2:
            await self._both_read.wait()
            return None
        return await self._repository.get_run_for_task(task_id)


class _FailOnceRepository:
    def __init__(self, repository, operation: str) -> None:
        self._repository = repository
        self._operation = operation
        self._failed = False

    def __getattr__(self, name: str):
        return getattr(self._repository, name)

    async def create_run(self, run):
        if self._operation == "create_run" and not self._failed:
            self._failed = True
            raise RuntimeError("injected_create_run_failure")
        return await self._repository.create_run(run)

    async def commit_agent_user_message(self, commit):
        if self._operation == "commit_user_message" and not self._failed:
            self._failed = True
            raise RuntimeError("injected_user_message_failure")
        return await self._repository.commit_agent_user_message(commit)


class _UnexpectedCreateConflictRepository:
    def __init__(self, repository) -> None:
        self._repository = repository
        self._hid_initial_read = False

    def __getattr__(self, name: str):
        return getattr(self._repository, name)

    async def get_run_for_task(self, task_id: str):
        if not self._hid_initial_read:
            self._hid_initial_read = True
            return None
        return await self._repository.get_run_for_task(task_id)

    async def create_run(self, _run):
        raise AgentStorageConflict("agent_run_write_unavailable")


class AgentSubmissionHandoffTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.engine = create_sqlite_engine(Path(self._tmpdir.name) / "handoff.sqlite3")
        self.sessions = create_sqlite_session_factory(self.engine)
        bootstrap_sqlite_database(self.engine)
        self.created_at = datetime(2026, 8, 26, 8, 0)
        with self.sessions.begin() as session:
            session.add_all(
                [
                    TaskRow(
                        task_id=f"task-{index}",
                        conversation_id=f"conversation-{index}",
                        root_message_id=f"message-{index}",
                        status="accepted",
                        routing_mode="auto",
                        created_at=self.created_at,
                        updated_at=self.created_at,
                    )
                    for index in range(1, 4)
                ]
            )
        self.storage = SQLiteStorage(self.sessions)
        self.agent_now = datetime(2026, 8, 26, 8, 1)
        self.repository = SQLiteAgentRepository(
            self.sessions,
            now_fn=lambda: self.agent_now,
        )
        self.events = _ExactEventStore()
        self.contexts = AgentInvocationContextStore()

    async def asyncTearDown(self) -> None:
        self.engine.dispose()
        self._tmpdir.cleanup()
        await super().asyncTearDown()

    def _request(self, **changes) -> AgentExecutionRequest:
        values = {
            "task_id": "task-1",
            "conversation_id": "conversation-1",
            "root_message_id": "message-1",
            "user_message": "hello",
            "owner_scope": "alice",
            "requested_capability_id": "skill.example",
            "metadata": {"skill_bundle_revision": "bundle-1"},
        }
        values.update(changes)
        return AgentExecutionRequest(**values)

    def _orchestrator(
        self,
        *,
        repository=None,
        binding: AgentModelBinding | None = None,
    ) -> tuple[AgentLoopOrchestrator, _WaitingRunner]:
        repository = repository or self.repository
        runner = _WaitingRunner(repository)
        selected_binding = binding or AgentModelBinding(
            "edition-a",
            option_digests={"options": "digest-a"},
        )

        def make_event(**values) -> EventRecord:
            return EventRecord(
                event_id="nondeterministic-placeholder",
                visibility=EventVisibility.FRONTEND,
                created_at=datetime.now(timezone.utc),
                **values,
            )

        return (
            AgentLoopOrchestrator(
                runs=repository,
                writer=repository,
                runner=runner,  # type: ignore[arg-type]
                final_output=_UnusedFinalOutput(),  # type: ignore[arg-type]
                contexts=self.contexts,
                task_loader=self.storage.get_task,
                task_cas=self.storage.compare_and_set_task,
                binding_factory=lambda _request: selected_binding,
                context_budget_factory=lambda _binding: (
                    AgentContextBudget.from_model_context_window(450_000)
                ),
                record_event=self.events.record,
                make_event=make_event,
                event_loader=self.events.load,
            ),
            runner,
        )

    async def test_initialize_is_exact_and_start_facade_runs_initialized_record(self) -> None:
        orchestrator, runner = self._orchestrator()
        request = self._request()

        first = await orchestrator.initialize_run(request)
        second = await orchestrator.initialize_run(request)

        self.assertEqual(first.run, second.run)
        self.assertEqual(first.run.run_id, "agent-run:task-1")
        items = await self.repository.list_items(first.run.run_id)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].sequence, 1)
        self.assertEqual(items[0].kind.value, "user_message")
        self.assertEqual(
            json.loads(items[0].payload_json)["context_budget"],
            AgentContextBudget.from_model_context_window(450_000).to_payload(),
        )
        self.assertEqual(
            set(self.events.events),
            {
                "evt-agent-task-graph-created:task-1",
                "evt-agent-run-started:agent-run:task-1",
            },
        )
        self.assertEqual(
            self.events.record_calls,
            [
                "evt-agent-task-graph-created:task-1",
                "evt-agent-run-started:agent-run:task-1",
            ],
        )
        self.assertTrue(
            all(task_id == "task-1" for task_id, _ in self.events.load_calls)
        )
        self.assertEqual(
            self.events.events["evt-agent-task-graph-created:task-1"].created_at,
            first.run.created_at,
        )
        self.assertEqual(
            self.events.events["evt-agent-run-started:agent-run:task-1"].created_at,
            first.run.created_at + timedelta(microseconds=1),
        )
        self.assertEqual(first.run.created_at, items[0].committed_at)
        self.assertEqual(runner.calls, [])

        result = await orchestrator.start_or_resume(request)

        self.assertEqual(result.state, "waiting")
        self.assertEqual([call[0] for call in runner.calls], [first.run.run_id])
        self.assertEqual(self.contexts.current_user_input(first.run), "hello")
        self.assertEqual(
            self.contexts.request_metadata(first.run)["agent_owner_scope"],
            "alice",
        )

    async def test_initialize_terminal_run_creates_no_items_events_or_execution(self) -> None:
        orchestrator, runner = self._orchestrator()
        request = self._request()

        first = await orchestrator.initialize_terminal_run(
            request,
            status=AgentRunStatus.FAILED,
            reason_code="agent_skill_bundle_revision_retired",
        )
        replayed = await orchestrator.initialize_terminal_run(
            request,
            status=AgentRunStatus.FAILED,
            reason_code="agent_skill_bundle_revision_retired",
        )

        self.assertEqual(replayed, first)
        self.assertEqual(first.status, AgentRunStatus.FAILED)
        self.assertEqual(first.next_item_sequence, 1)
        self.assertEqual(await self.repository.list_items(first.run_id), ())
        self.assertEqual(self.events.events, {})
        self.assertEqual(runner.calls, [])
        task = await self.storage.get_task(request.task_id)
        assert task is not None
        self.assertEqual(task.status, TaskStatus.FAILED)

    async def test_completed_task_authority_terminalizes_only_the_run(self) -> None:
        orchestrator, _ = self._orchestrator()
        initialized = await orchestrator.initialize_run(self._request())
        task = await self.storage.get_task(initialized.task.task_id)
        assert task is not None
        completed_task = await self.storage.compare_and_set_task(
            replace(task, status=TaskStatus.COMPLETED),
            expected_from_status=TaskStatus.RUNNING,
        )
        assert completed_task is not None
        items_before = await self.repository.list_items(initialized.run.run_id)
        events_before = list(self.events.record_calls)

        completed = await orchestrator.complete_from_terminal_task(
            initialized.task.task_id,
            reason_code="agent_terminal_task_completed_run_convergence",
        )

        assert completed is not None
        self.assertEqual(completed.status, AgentRunStatus.COMPLETED)
        self.assertEqual(
            completed.terminal_reason_code,
            "agent_terminal_task_completed_run_convergence",
        )
        self.assertEqual(
            await self.repository.list_items(initialized.run.run_id),
            items_before,
        )
        self.assertEqual(self.events.record_calls, events_before)

    async def test_initialize_rejects_task_run_binding_and_user_identity_drift(self) -> None:
        orchestrator, _ = self._orchestrator()
        await orchestrator.initialize_run(self._request())

        for request in (
            self._request(conversation_id="conversation-other"),
            self._request(root_message_id="message-other"),
            self._request(user_message="changed"),
        ):
            with self.subTest(request=request):
                with self.assertRaises(AgentStorageConflict):
                    await orchestrator.initialize_run(request)

        changed_binding, _ = self._orchestrator(
            binding=AgentModelBinding("edition-b")
        )
        with self.assertRaisesRegex(
            AgentStorageConflict,
            "agent_run_binding_mismatch",
        ):
            await changed_binding.initialize_run(self._request())

    async def test_initialize_repairs_each_event_fault_gap_with_stable_identity(self) -> None:
        orchestrator, _ = self._orchestrator()
        self.events.fail_once_for_type = "agent.run.started"

        with self.assertRaisesRegex(RuntimeError, "injected_event_write_failure"):
            await orchestrator.initialize_run(self._request())

        run = await self.repository.get_run_for_task("task-1")
        assert run is not None
        self.assertEqual(
            set(self.events.events),
            {"evt-agent-task-graph-created:task-1"},
        )

        initialized = await orchestrator.initialize_run(self._request())
        items = await self.repository.list_items(run.run_id)

        self.assertEqual(initialized.run, run)
        self.assertEqual(len(self.events.events), 2)
        self.assertEqual(
            self.events.events["evt-agent-task-graph-created:task-1"].created_at,
            run.created_at,
        )
        self.assertEqual(
            self.events.events["evt-agent-run-started:agent-run:task-1"].created_at,
            run.created_at + timedelta(microseconds=1),
        )
        self.assertEqual(run.created_at, items[0].committed_at)

    async def test_initialize_repairs_task_run_and_user_item_fault_gaps(self) -> None:
        create_fault = _FailOnceRepository(self.repository, "create_run")
        orchestrator, _ = self._orchestrator(repository=create_fault)
        request = self._request(
            task_id="task-2",
            conversation_id="conversation-2",
            root_message_id="message-2",
        )

        with self.assertRaisesRegex(RuntimeError, "injected_create_run_failure"):
            await orchestrator.initialize_run(request)
        task = await self.storage.get_task("task-2")
        assert task is not None
        self.assertEqual(str(task.status), "running")
        self.assertIsNone(await self.repository.get_run_for_task("task-2"))
        created = await orchestrator.initialize_run(request)
        self.assertEqual(created.run.run_id, "agent-run:task-2")

        user_item_fault = _FailOnceRepository(
            self.repository,
            "commit_user_message",
        )
        orchestrator, _ = self._orchestrator(repository=user_item_fault)
        request = self._request(
            task_id="task-3",
            conversation_id="conversation-3",
            root_message_id="message-3",
        )

        with self.assertRaisesRegex(RuntimeError, "injected_user_message_failure"):
            await orchestrator.initialize_run(request)
        run = await self.repository.get_run_for_task("task-3")
        assert run is not None
        self.assertEqual(await self.repository.list_items(run.run_id), ())
        replayed = await orchestrator.initialize_run(request)
        self.assertEqual(replayed.run.run_id, run.run_id)
        self.assertEqual(len(await self.repository.list_items(run.run_id)), 1)

    async def test_event_loader_rejects_drift_without_overwrite(self) -> None:
        orchestrator, _ = self._orchestrator()
        initialized = await orchestrator.initialize_run(self._request())
        event_id = "evt-agent-run-started:agent-run:task-1"
        drifted = replace(
            self.events.events[event_id],
            payload={"routing_mode": "changed"},
        )
        self.events.events[event_id] = drifted

        with self.assertRaisesRegex(
            AgentStorageConflict,
            "agent_initialization_event_conflict",
        ):
            await orchestrator.initialize_run(self._request())

        self.assertEqual(self.events.events[event_id], drifted)
        self.assertEqual(initialized.run.run_id, "agent-run:task-1")

    async def test_two_workers_re_read_the_exact_run_after_create_race(self) -> None:
        race_repository = _CreateRaceRepository(self.repository)
        first, _ = self._orchestrator(repository=race_repository)
        second, _ = self._orchestrator(repository=race_repository)

        results = await asyncio.gather(
            first.initialize_run(self._request()),
            second.initialize_run(self._request()),
        )

        self.assertEqual(results[0].run, results[1].run)
        self.assertEqual(results[0].run.run_id, "agent-run:task-1")
        self.assertEqual(len(await self.repository.list_items(results[0].run.run_id)), 1)
        self.assertEqual(len(self.events.events), 2)

    async def test_create_does_not_swallow_unrelated_storage_conflict(self) -> None:
        binding = AgentModelBinding(
            "edition-a",
            option_digests={"options": "digest-a"},
        )
        await self.repository.create_run(
            AgentRun(
                run_id="agent-run:task-1",
                task_id="task-1",
                conversation_id="conversation-1",
                status=AgentRunStatus.RUNNING,
                binding=binding,
            )
        )
        repository = _UnexpectedCreateConflictRepository(self.repository)
        orchestrator, _ = self._orchestrator(
            repository=repository,
            binding=binding,
        )

        with self.assertRaisesRegex(
            AgentStorageConflict,
            "agent_run_write_unavailable",
        ):
            await orchestrator.initialize_run(self._request())

        self.assertEqual(await self.repository.list_items("agent-run:task-1"), ())
