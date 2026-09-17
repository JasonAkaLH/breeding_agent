from __future__ import annotations

import inspect
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import func, select

from src.orchestration.agent_loop.final_output import AgentFinalOutputPublisher
from src.orchestration.agent_loop.lease import AgentLeaseController
from src.orchestration.agent_loop.models import (
    AgentFinishMetadata,
    AgentModelBinding,
    AgentRun,
    AgentRunStatus,
    AgentSample,
    AgentSampleCommit,
    AgentStorageConflict,
    AgentToolCall,
    AgentUsage,
)
from src.storage.sqlite import (
    SQLiteAgentRepository,
    bootstrap_sqlite_database,
    create_sqlite_engine,
    create_sqlite_session_factory,
)
from src.storage.sqlite.models import (
    AgentFinalReceiptRow,
    ArtifactRow,
    EventRecordRow,
    MessageRow,
    TaskNodeRow,
    TaskRow,
)


class AgentFinalOutputPublisherTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.engine = create_sqlite_engine(Path(self.temp_dir.name) / "final.sqlite")
        self.sessions = create_sqlite_session_factory(self.engine)
        bootstrap_sqlite_database(self.engine)
        with self.sessions.begin() as session:
            session.add(
                TaskRow(
                    task_id="task-1",
                    conversation_id="conv-1",
                    root_message_id="message-root",
                    status="running",
                    routing_mode="auto",
                )
            )
        self.repository = SQLiteAgentRepository(self.sessions)
        self.binding = AgentModelBinding("edition-a")
        await self.repository.create_run(
            AgentRun("run-1", "task-1", "conv-1", AgentRunStatus.RUNNING, self.binding)
        )
        self.leases = AgentLeaseController(self.repository, ttl_seconds=30)
        self.handle = await self.leases.acquire("run-1", owner_id="worker")

    async def asyncTearDown(self) -> None:
        self.engine.dispose()
        self.temp_dir.cleanup()

    async def _candidate(self, text="final answer", calls=(), mixed=False):
        run = await self.repository.get_run("run-1")
        return await self.repository.commit_agent_sample(
            AgentSampleCommit(
                "run-1",
                run.revision,
                self.handle.current.token,
                AgentSample(
                    "sample-final",
                    self.binding,
                    text,
                    tuple(calls),
                    AgentUsage(status="usage_unavailable"),
                    AgentFinishMetadata("stop", 1, mixed_text_and_tool_calls=mixed),
                ),
                {call.provider_safe_name: "skill.one" for call in calls},
            )
        )

    async def test_publishes_one_projection_and_exact_retry_without_model(self) -> None:
        for index in range(40):
            run = await self.repository.get_run("run-1")
            await self.repository.commit_agent_sample(
                AgentSampleCommit(
                    "run-1",
                    run.revision,
                    self.handle.current.token,
                    AgentSample(
                        f"history-{index}",
                        self.binding,
                        f"history {index}",
                        (),
                        AgentUsage(status="usage_unavailable"),
                        AgentFinishMetadata("stop", 1),
                    ),
                    {},
                )
            )
        candidate = await self._candidate()
        cleanup_statuses = []

        class Cleaner:
            def cleanup_terminal(_self, *, run, items):
                cleanup_statuses.append((run.status, len(items)))
                return 0

        publisher = AgentFinalOutputPublisher(
            runs=self.repository,
            writer=self.repository,
            lease_controller=self.leases,
            transient_result_cleaner=Cleaner(),
        )
        result = await publisher.publish(
            run_id="run-1",
            candidate_item_id=candidate.assistant_item.item_id,
            handle=self.handle,
        )
        retry = await publisher.publish(
            run_id="run-1",
            candidate_item_id=candidate.assistant_item.item_id,
            handle=self.handle,
        )
        self.assertEqual(retry, result)
        self.assertEqual(result.run.status, AgentRunStatus.COMPLETED)
        self.assertEqual(
            [status for status, _count in cleanup_statuses],
            [AgentRunStatus.COMPLETED, AgentRunStatus.COMPLETED],
        )
        with self.sessions() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(AgentFinalReceiptRow)), 1)
            self.assertEqual(session.scalar(select(func.count()).select_from(MessageRow)), 1)
            self.assertEqual(session.scalar(select(func.count()).select_from(EventRecordRow)), 1)
            self.assertEqual(session.scalar(select(func.count()).select_from(ArtifactRow)), 1)
            self.assertEqual(session.scalar(select(func.count()).select_from(TaskNodeRow)), 1)
        source = inspect.getsource(AgentFinalOutputPublisher)
        self.assertNotIn("AgentModel", source)
        self.assertNotIn("executor", source.lower())
        self.assertNotIn("catalog", source.lower())

    async def test_mixed_text_and_calls_is_not_publishable(self) -> None:
        call = AgentToolCall("call-1", "tool_one", "{}", 0)
        candidate = await self._candidate("must ignore", (call,), mixed=True)
        publisher = AgentFinalOutputPublisher(
            runs=self.repository,
            writer=self.repository,
            lease_controller=self.leases,
        )
        with self.assertRaisesRegex(AgentStorageConflict, "candidate_invalid"):
            await publisher.publish(
                run_id="run-1",
                candidate_item_id=candidate.assistant_item.item_id,
                handle=self.handle,
            )

    async def test_projection_fault_rolls_back_and_retry_can_publish_once(self) -> None:
        candidate = await self._candidate()

        def fault(stage: str) -> None:
            if stage == "final_after_projection":
                raise RuntimeError("injected final fault")

        faulty = SQLiteAgentRepository(self.sessions, fault_injector=fault)
        publisher = AgentFinalOutputPublisher(
            runs=faulty,
            writer=faulty,
            lease_controller=self.leases,
        )
        with self.assertRaisesRegex(RuntimeError, "injected final fault"):
            await publisher.publish(
                run_id="run-1",
                candidate_item_id=candidate.assistant_item.item_id,
                handle=self.handle,
            )
        self.assertEqual((await self.repository.get_run("run-1")).status, AgentRunStatus.RUNNING)
        with self.sessions() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(AgentFinalReceiptRow)), 0)
            self.assertEqual(session.scalar(select(func.count()).select_from(MessageRow)), 0)
        class FailingCleaner:
            def cleanup_terminal(self, **_kwargs):
                raise RuntimeError("cleanup failed")

        recovered = AgentFinalOutputPublisher(
            runs=self.repository,
            writer=self.repository,
            lease_controller=self.leases,
            transient_result_cleaner=FailingCleaner(),
        )
        result = await recovered.publish(
            run_id="run-1",
            candidate_item_id=candidate.assistant_item.item_id,
            handle=self.handle,
        )
        self.assertEqual(result.run.status, AgentRunStatus.COMPLETED)
