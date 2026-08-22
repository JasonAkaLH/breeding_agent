from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import func, select

from src.orchestration.agent_loop.models import (
    AgentCallOutcomeCommit,
    AgentCallOutcomeStatus,
    AgentFinishMetadata,
    AgentFinalOutputCommit,
    AgentItemKind,
    AgentItemState,
    AgentModelBinding,
    AgentRun,
    AgentRunStatus,
    AgentSample,
    AgentSampleCommit,
    AgentStorageConflict,
    AgentStagedArtifact,
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
    AgentItemRow,
    AgentRunRow,
    ArtifactRow,
    EventRecordRow,
    MessageRow,
    TaskNodeRow,
    TaskRow,
)


class SQLiteAgentStorageTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.engine = create_sqlite_engine(Path(self._tmpdir.name) / "agent.sqlite3")
        self.session_factory = create_sqlite_session_factory(self.engine)
        bootstrap_sqlite_database(self.engine)
        self.repository = SQLiteAgentRepository(self.session_factory)
        self._seed_task("task-1", "conv-1")

    async def asyncTearDown(self) -> None:
        self.engine.dispose()
        self._tmpdir.cleanup()
        await super().asyncTearDown()

    def _seed_task(self, task_id: str, conversation_id: str) -> None:
        with self.session_factory() as session:
            session.add(
                TaskRow(
                    task_id=task_id,
                    conversation_id=conversation_id,
                    root_message_id=f"message-{task_id}",
                    status="accepted",
                    routing_mode="auto",
                    requested_capability_id=None,
                    root_node_id=None,
                    summary=None,
                    cancel_requested_at=None,
                    created_at=None,
                    updated_at=None,
                    mcp_execution_mode=None,
                    mcp_shadow_enabled=None,
                    mcp_rollout_config_version=None,
                    mcp_route_reason_code=None,
                    mcp_rollout_mode=None,
                )
            )
            session.commit()

    def _run(self, *, run_id="run-1", task_id="task-1", conversation_id="conv-1") -> AgentRun:
        return AgentRun(
            run_id=run_id,
            task_id=task_id,
            conversation_id=conversation_id,
            status=AgentRunStatus.RUNNING,
            binding=AgentModelBinding("edition-a", reasoning_effort="high", option_digests={"policy": "abc"}),
        )

    def _sample(self, *calls: AgentToolCall, sample_id="sample-1", text="") -> AgentSample:
        return AgentSample(
            sample_id=sample_id,
            binding=self._run().binding,
            visible_text=text,
            tool_calls=tuple(calls),
            usage=AgentUsage(status="usage_unavailable"),
            finish=AgentFinishMetadata(finish_reason="tool_calls" if calls else "stop", attempts=1),
        )

    async def _create(self) -> AgentRun:
        return await self.repository.create_run(self._run())

    async def _commit_two_calls(self):
        run = await self._create()
        sample = self._sample(
            AgentToolCall("call-1", "tool_one", "{}", 0),
            AgentToolCall("call-2", "tool_two", '{"x":1}', 1),
        )
        return await self.repository.commit_agent_sample(
            AgentSampleCommit(
                run_id=run.run_id,
                expected_revision=run.revision,
                expected_claim_token=None,
                sample=sample,
                capability_ids_by_tool_name={"tool_one": "skill.one", "tool_two": "mcp.dispatch"},
            )
        )

    async def test_task_has_one_run_and_item_sequence_is_unique_monotonic(self) -> None:
        await self._create()
        with self.assertRaisesRegex(AgentStorageConflict, "already_bound"):
            await self.repository.create_run(self._run(run_id="run-duplicate"))

        committed = await self.repository.commit_agent_sample(
            AgentSampleCommit("run-1", 0, None, self._sample(text="draft"), {})
        )
        items = await self.repository.list_items("run-1")
        self.assertEqual([item.sequence for item in items], [1])
        self.assertEqual(committed.run.next_item_sequence, 2)

    async def test_sample_atomically_persists_calls_result_slots_and_nodes_before_executor(self) -> None:
        executor_calls = 0
        committed = await self._commit_two_calls()
        with self.session_factory() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(TaskNodeRow)), 2)
        self.assertEqual(executor_calls, 0)
        self.assertEqual([item.state for item in committed.result_reservations], [AgentItemState.RESERVED] * 2)
        self.assertEqual(
            [item.kind for item in await self.repository.list_items("run-1")],
            [
                AgentItemKind.ASSISTANT_MESSAGE,
                AgentItemKind.TOOL_CALL,
                AgentItemKind.TOOL_RESULT,
                AgentItemKind.TOOL_CALL,
                AgentItemKind.TOOL_RESULT,
            ],
        )
        self.assertEqual([item.sequence for item in await self.repository.list_items("run-1")], [1, 2, 3, 4, 5])

    async def test_sample_fault_rolls_back_items_nodes_and_run_revision(self) -> None:
        await self._create()

        def fail(stage: str) -> None:
            if stage == "sample_after_items":
                raise RuntimeError("injected")

        repository = SQLiteAgentRepository(self.session_factory, fault_injector=fail)
        with self.assertRaisesRegex(RuntimeError, "injected"):
            await repository.commit_agent_sample(
                AgentSampleCommit(
                    "run-1",
                    0,
                    None,
                    self._sample(AgentToolCall("call-1", "tool_one", "{}", 0)),
                    {"tool_one": "skill.one"},
                )
            )
        self.assertEqual(await self.repository.list_items("run-1"), ())
        self.assertEqual((await self.repository.get_run("run-1")).revision, 0)
        with self.session_factory() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(TaskNodeRow)), 0)

    async def test_result_must_reference_reserved_call_and_duplicate_terminal_is_rejected(self) -> None:
        committed = await self._commit_two_calls()
        with self.assertRaisesRegex(AgentStorageConflict, "call_item_missing"):
            await self.repository.commit_agent_call_outcome(
                AgentCallOutcomeCommit("run-1", committed.run.revision, None, "missing", {}, AgentCallOutcomeStatus.COMPLETED)
            )
        result = await self.repository.commit_agent_call_outcome(
            AgentCallOutcomeCommit(
                "run-1",
                committed.run.revision,
                None,
                committed.call_items[0].item_id,
                {"answer": "ok"},
                AgentCallOutcomeStatus.COMPLETED,
                staged_artifacts=(AgentStagedArtifact("artifact-1", "json", "staged://artifact-1"),),
            )
        )
        self.assertEqual(result.state, AgentItemState.COMMITTED)
        with self.session_factory() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(ArtifactRow)), 1)
        with self.assertRaises(AgentStorageConflict):
            await self.repository.commit_agent_call_outcome(
                AgentCallOutcomeCommit(
                    "run-1",
                    committed.run.revision + 1,
                    None,
                    committed.call_items[0].item_id,
                    {"answer": "again"},
                    AgentCallOutcomeStatus.COMPLETED,
                )
            )

    async def test_provider_call_id_can_repeat_in_a_later_sample_without_identity_collision(self) -> None:
        run = await self._create()
        first = await self.repository.commit_agent_sample(
            AgentSampleCommit(
                "run-1",
                run.revision,
                None,
                self._sample(AgentToolCall("provider-call", "tool_one", "{}", 0), sample_id="sample-a"),
                {"tool_one": "skill.one"},
            )
        )
        await self.repository.commit_agent_call_outcome(
            AgentCallOutcomeCommit(
                "run-1", first.run.revision, None, first.call_items[0].item_id, {"ok": 1}, AgentCallOutcomeStatus.COMPLETED
            )
        )
        run = await self.repository.get_run("run-1")
        second = await self.repository.commit_agent_sample(
            AgentSampleCommit(
                "run-1",
                run.revision,
                None,
                self._sample(AgentToolCall("provider-call", "tool_one", "{}", 0), sample_id="sample-b"),
                {"tool_one": "skill.one"},
            )
        )
        self.assertNotEqual(first.call_items[0].item_id, second.call_items[0].item_id)
        self.assertEqual([item.sequence for item in await self.repository.list_items("run-1")], [1, 2, 3, 4, 5, 6])

    async def test_multiple_waiting_calls_remain_consistent_and_resume_outcomes_remove_each(self) -> None:
        committed = await self._commit_two_calls()
        await self.repository.commit_agent_call_outcome(
            AgentCallOutcomeCommit(
                "run-1", committed.run.revision, None, committed.call_items[0].item_id, {"prompt": "a"}, AgentCallOutcomeStatus.WAITING_FOR_INPUT
            )
        )
        run = await self.repository.get_run("run-1")
        self.assertEqual(run.status, AgentRunStatus.WAITING_FOR_INPUT)
        second_result = await self.repository.commit_agent_call_outcome(
            AgentCallOutcomeCommit(
                "run-1", run.revision, None, committed.call_items[1].item_id, {"remote": "pending"}, AgentCallOutcomeStatus.WAITING_FOR_DEPENDENCY
            )
        )
        run = await self.repository.get_run("run-1")
        self.assertEqual(set(run.waiting_call_item_ids), {item.item_id for item in committed.call_items})
        self.assertEqual(second_result.state, AgentItemState.RESERVED)

        await self.repository.commit_agent_call_outcome(
            AgentCallOutcomeCommit(
                "run-1", run.revision, None, committed.call_items[0].item_id, {"answer": "a"}, AgentCallOutcomeStatus.COMPLETED
            )
        )
        run = await self.repository.get_run("run-1")
        self.assertEqual(run.status, AgentRunStatus.WAITING_FOR_DEPENDENCY)
        self.assertEqual(run.waiting_call_item_ids, (committed.call_items[1].item_id,))
        await self.repository.commit_agent_call_outcome(
            AgentCallOutcomeCommit(
                "run-1", run.revision, None, committed.call_items[1].item_id, {"answer": "b"}, AgentCallOutcomeStatus.COMPLETED
            )
        )
        run = await self.repository.get_run("run-1")
        self.assertEqual(run.status, AgentRunStatus.RUNNING)
        self.assertEqual(run.waiting_call_item_ids, ())

    async def test_final_commit_is_atomic_deterministic_and_exactly_idempotent(self) -> None:
        run = await self._create()
        sample = await self.repository.commit_agent_sample(
            AgentSampleCommit("run-1", run.revision, None, self._sample(text="candidate"), {})
        )
        final = await self.repository.commit_agent_final_output(
            AgentFinalOutputCommit("run-1", sample.run.revision, None, "最终答案")
        )
        repeated = await self.repository.commit_agent_final_output(
            AgentFinalOutputCommit("run-1", sample.run.revision, None, "最终答案")
        )
        self.assertEqual(final, repeated)
        self.assertEqual(final.run.status, AgentRunStatus.COMPLETED)
        with self.session_factory() as session:
            for row_type in (AgentFinalReceiptRow, ArtifactRow, MessageRow, EventRecordRow):
                self.assertEqual(session.scalar(select(func.count()).select_from(row_type)), 1)
            self.assertEqual(session.get(TaskRow, "task-1").status, "completed")
        with self.assertRaisesRegex(AgentStorageConflict, "conflict"):
            await self.repository.commit_agent_final_output(
                AgentFinalOutputCommit("run-1", sample.run.revision, None, "不同答案")
            )

    async def test_outcome_fault_never_exposes_terminal_node_without_result(self) -> None:
        committed = await self._commit_two_calls()

        def fail(stage: str) -> None:
            if stage == "outcome_after_result":
                raise RuntimeError("injected")

        repository = SQLiteAgentRepository(self.session_factory, fault_injector=fail)
        with self.assertRaisesRegex(RuntimeError, "injected"):
            await repository.commit_agent_call_outcome(
                AgentCallOutcomeCommit(
                    "run-1",
                    committed.run.revision,
                    None,
                    committed.call_items[0].item_id,
                    {"answer": "hidden"},
                    AgentCallOutcomeStatus.COMPLETED,
                    staged_artifacts=(AgentStagedArtifact("artifact-hidden", "json", "staged://hidden"),),
                    continuation_payload={
                        "authority_digest": "d" * 64,
                        "schema": "maf.agent.continuation.v1",
                    },
                )
            )
        items = await self.repository.list_items("run-1")
        result = next(item for item in items if item.source_call_item_id == committed.call_items[0].item_id)
        self.assertEqual(result.state, AgentItemState.RESERVED)
        self.assertFalse(any(item.kind is AgentItemKind.CONTINUATION for item in items))
        with self.session_factory() as session:
            node = session.get(TaskNodeRow, committed.node_ids[0])
            self.assertEqual(node.status, "pending")
            self.assertEqual(session.scalar(select(func.count()).select_from(ArtifactRow)), 0)

    async def test_final_fault_rolls_back_every_projection_and_receipt(self) -> None:
        run = await self._create()
        sample = await self.repository.commit_agent_sample(
            AgentSampleCommit("run-1", run.revision, None, self._sample(text="candidate"), {})
        )

        def fail(stage: str) -> None:
            if stage == "final_after_projection":
                raise RuntimeError("injected")

        repository = SQLiteAgentRepository(self.session_factory, fault_injector=fail)
        with self.assertRaisesRegex(RuntimeError, "injected"):
            await repository.commit_agent_final_output(
                AgentFinalOutputCommit("run-1", sample.run.revision, None, "hidden final")
            )
        with self.session_factory() as session:
            for row_type in (AgentFinalReceiptRow, ArtifactRow, MessageRow, EventRecordRow, TaskNodeRow):
                self.assertEqual(session.scalar(select(func.count()).select_from(row_type)), 0)
            self.assertEqual(session.get(TaskRow, "task-1").status, "accepted")
        self.assertEqual((await self.repository.get_run("run-1")).status, AgentRunStatus.RUNNING)

    async def test_waiting_mismatch_reconciles_to_fatal_consistency_error(self) -> None:
        committed = await self._commit_two_calls()
        await self.repository.commit_agent_call_outcome(
            AgentCallOutcomeCommit(
                "run-1", committed.run.revision, None, committed.call_items[0].item_id, {"prompt": "a"}, AgentCallOutcomeStatus.WAITING_FOR_INPUT
            )
        )
        with self.session_factory() as session:
            row = session.get(AgentRunRow, "run-1")
            row.waiting_call_item_ids = []
            session.commit()

        reconciled = await self.repository.reconcile_agent_run_consistency("run-1")

        self.assertEqual(reconciled.status, AgentRunStatus.FAILED)
        with self.session_factory() as session:
            self.assertEqual(session.get(TaskRow, "task-1").status, "failed")

    async def test_fatal_terminal_closes_nodes_clears_claim_and_records_safe_reason(self) -> None:
        committed = await self._commit_two_calls()
        failed = await self.repository.fail_agent_run(
            "run-1",
            expected_revision=committed.run.revision,
            expected_claim_token=None,
            safe_error_code="storage_consistency_error",
        )
        self.assertEqual(failed.status, AgentRunStatus.FAILED)
        self.assertEqual(failed.terminal_reason_code, "storage_consistency_error")
        self.assertIsNone(failed.claim_token)
        with self.session_factory() as session:
            self.assertEqual(session.get(TaskRow, "task-1").status, "failed")
            self.assertEqual(
                set(session.scalars(select(TaskNodeRow.status).where(TaskNodeRow.task_id == "task-1")).all()),
                {"failed"},
            )

    async def test_cancel_terminal_is_closed_and_cas_guarded(self) -> None:
        run = await self._create()
        cancelled = await self.repository.cancel_agent_run(
            "run-1",
            expected_revision=run.revision,
            expected_claim_token=None,
            safe_reason_code="user_cancelled",
        )
        self.assertEqual(cancelled.status, AgentRunStatus.CANCELLED)
        self.assertEqual(cancelled.terminal_reason_code, "user_cancelled")
        with self.assertRaises(AgentStorageConflict):
            await self.repository.cancel_agent_run(
                "run-1",
                expected_revision=cancelled.revision,
                expected_claim_token=None,
                safe_reason_code="duplicate",
            )
