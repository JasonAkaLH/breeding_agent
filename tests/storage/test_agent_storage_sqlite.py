from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sqlalchemy import func, select

from src.integrations.agent_skills.public_profile import PublicSkillProfile
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
    AgentUserMessageCommit,
)
from src.orchestration.agent_loop.skill_activation import (
    build_canonical_skill_activation,
)
from src.orchestration.agent_loop.result_artifacts import (
    AgentSkillResultArtifactStager,
    parse_skill_result_storage_ref,
)
from src.orchestration.agent_loop.result_projection import AgentCallResultProjector
from src.storage.artifact_files import LocalArtifactFileStore
from src.storage.sqlite import (
    SQLiteAgentRepository,
    bootstrap_sqlite_database,
    create_sqlite_engine,
    create_sqlite_session_factory,
)
from src.storage.sqlite.models import (
    AgentFinalReceiptRow,
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

    async def test_initial_user_message_is_durable_idempotent_and_precedes_samples(self) -> None:
        run = await self._create()
        initialized = await self.repository.commit_agent_user_message(
            AgentUserMessageCommit(run.run_id, run.revision, None, "用户问题")
        )
        duplicate = await self.repository.commit_agent_user_message(
            AgentUserMessageCommit(
                run.run_id,
                initialized.run.revision,
                None,
                "用户问题",
            )
        )

        self.assertEqual(initialized.item, duplicate.item)
        self.assertEqual(initialized.item.kind, AgentItemKind.USER_MESSAGE)
        self.assertEqual(initialized.run.next_item_sequence, 2)
        committed = await self.repository.commit_agent_sample(
            AgentSampleCommit(
                run.run_id,
                initialized.run.revision,
                None,
                self._sample(text="回答"),
                {},
            )
        )
        self.assertEqual(
            [item.kind for item in await self.repository.list_items(run.run_id)],
            [AgentItemKind.USER_MESSAGE, AgentItemKind.ASSISTANT_MESSAGE],
        )
        self.assertEqual(committed.assistant_item.sequence, 2)

    async def test_initial_user_and_hint_activation_commit_atomically_and_replay_exactly(self) -> None:
        run = await self._create()
        activation = build_canonical_skill_activation(
            binding_mode="hint",
            profile=PublicSkillProfile(
                capability_id="skill.report",
                name="report",
                display_name="Report",
                description="safe",
                triggers=(),
            ),
            pinned_bundle_revision="revision-1",
            resolved_bundle_revision="revision-1",
        )
        commit = AgentUserMessageCommit(
            run.run_id,
            run.revision,
            None,
            "用户问题",
            activation.payload_json,
            activation.payload_sha256,
        )

        initialized = await self.repository.commit_agent_user_message(commit)
        replay = await self.repository.commit_agent_user_message(
            AgentUserMessageCommit(
                run.run_id,
                initialized.run.revision,
                None,
                "用户问题",
                activation.payload_json,
                activation.payload_sha256,
            )
        )

        assert initialized.activation_item is not None
        self.assertEqual(replay, initialized)
        self.assertEqual(initialized.run.next_item_sequence, 3)
        self.assertEqual(initialized.item.committed_at, initialized.activation_item.committed_at)
        self.assertEqual(
            [item.kind for item in await self.repository.list_items(run.run_id)],
            [AgentItemKind.USER_MESSAGE, AgentItemKind.SKILL_ACTIVATION],
        )
        with self.assertRaisesRegex(AgentStorageConflict, "presence_conflict"):
            await self.repository.commit_agent_user_message(
                AgentUserMessageCommit(
                    run.run_id,
                    initialized.run.revision,
                    None,
                    "用户问题",
                )
            )

    async def test_initial_hint_faults_roll_back_user_activation_and_revision(self) -> None:
        activation = build_canonical_skill_activation(
            binding_mode="hint",
            profile=PublicSkillProfile(
                capability_id="skill.report",
                name="report",
                display_name="Report",
                description="safe",
                triggers=(),
            ),
            pinned_bundle_revision="revision-1",
            resolved_bundle_revision="revision-1",
        )
        for index, stage in enumerate(
            (
                "user_initial_after_user",
                "user_initial_after_activation",
                "user_initial_after_run_update",
            ),
            start=2,
        ):
            with self.subTest(stage=stage):
                task_id = f"task-{index}"
                run_id = f"run-{index}"
                self._seed_task(task_id, "conv-1")
                run = await self.repository.create_run(
                    self._run(run_id=run_id, task_id=task_id)
                )

                def fail(current: str) -> None:
                    if current == stage:
                        raise RuntimeError("injected")

                repository = SQLiteAgentRepository(
                    self.session_factory,
                    fault_injector=fail,
                )
                with self.assertRaisesRegex(RuntimeError, "injected"):
                    await repository.commit_agent_user_message(
                        AgentUserMessageCommit(
                            run.run_id,
                            run.revision,
                            None,
                            "用户问题",
                            activation.payload_json,
                            activation.payload_sha256,
                        )
                    )
                stored = await self.repository.get_run(run_id)
                assert stored is not None
                self.assertEqual(stored.revision, 0)
                self.assertEqual(stored.next_item_sequence, 1)
                self.assertEqual(await self.repository.list_items(run_id), ())

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

    async def test_skill_result_artifact_and_terminal_node_commit_in_one_exact_cas(self) -> None:
        committed = await self._commit_two_calls()
        call_item = committed.call_items[0]
        projection = AgentCallResultProjector().project(
            capability_id="skill.one",
            output_payload={"articles": ["x" * 10_000 for _ in range(20)]},
            call_item_id=call_item.item_id,
            outcome="completed",
            safe_error_code=None,
        )
        self.assertTrue(projection.spill_required)
        file_store = LocalArtifactFileStore(Path(self._tmpdir.name) / "artifacts")
        staged = AgentSkillResultArtifactStager(
            file_store=file_store,
            manifest_root=Path(self._tmpdir.name) / "manifests",
        ).stage(
            run=committed.run,
            call_item=call_item,
            node_id=committed.node_ids[0],
            canonical_raw_bytes=projection.canonical_raw_bytes,
            raw_sha256=projection.raw_sha256,
            projection_revision=projection.projection_revision,
            expected_artifact_id=projection.spill_artifact_id,
        )
        outcome = AgentCallOutcomeCommit(
            "run-1",
            committed.run.revision,
            None,
            call_item.item_id,
            projection.safe_result_payload,
            AgentCallOutcomeStatus.COMPLETED,
            staged_artifacts=(staged,),
        )

        first = await self.repository.commit_agent_call_outcome(outcome)
        replay = await self.repository.commit_agent_call_outcome(outcome)

        self.assertEqual(replay, first)
        with self.session_factory() as session:
            node = session.get(TaskNodeRow, committed.node_ids[0])
            artifact = session.get(ArtifactRow, staged.artifact_id)
            self.assertEqual(node.status, "completed")
            self.assertEqual(node.output_refs, [staged.artifact_id, first.item_id])
            self.assertIsNotNone(artifact)
            metadata = parse_skill_result_storage_ref(artifact.storage_ref)
            self.assertEqual(metadata["call_item_id"], call_item.item_id)
            self.assertEqual(metadata["raw_sha256"], projection.raw_sha256)

        drifted = AgentStagedArtifact(
            staged.artifact_id,
            staged.artifact_type,
            staged.storage_ref.replace(committed.node_ids[0], "drift-node"),
            staged.summary,
        )
        with self.assertRaises(AgentStorageConflict):
            await self.repository.commit_agent_call_outcome(
                AgentCallOutcomeCommit(
                    "run-1",
                    committed.run.revision,
                    None,
                    call_item.item_id,
                    projection.safe_result_payload,
                    AgentCallOutcomeStatus.COMPLETED,
                    staged_artifacts=(drifted,),
                )
            )

    async def test_projection_failure_commits_failed_node_and_result_together(self) -> None:
        committed = await self._commit_two_calls()
        result = await self.repository.commit_agent_call_outcome(
            AgentCallOutcomeCommit(
                "run-1",
                committed.run.revision,
                None,
                committed.call_items[0].item_id,
                None,
                AgentCallOutcomeStatus.FAILED,
                safe_error_code="agent_result_invalid",
            )
        )

        self.assertEqual(result.state, AgentItemState.COMMITTED)
        with self.session_factory() as session:
            node = session.get(TaskNodeRow, committed.node_ids[0])
            self.assertEqual(node.status, "failed")
            self.assertEqual(node.output_refs, [result.item_id])
        self.assertIn("agent_result_invalid", result.payload_json)

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

    async def test_skill_result_stage_remains_private_when_outcome_cas_rolls_back(self) -> None:
        committed = await self._commit_two_calls()
        call = committed.call_items[0]
        projection = AgentCallResultProjector().project(
            capability_id="skill.one",
            output_payload={"rows": ["x" * 10_000 for _ in range(20)]},
            call_item_id=call.item_id,
            outcome="completed",
            safe_error_code=None,
        )
        file_store = LocalArtifactFileStore(Path(self._tmpdir.name) / "private-artifacts")
        manifest_root = Path(self._tmpdir.name) / "private-manifests"
        staged = AgentSkillResultArtifactStager(
            file_store=file_store,
            manifest_root=manifest_root,
        ).stage(
            run=committed.run,
            call_item=call,
            node_id=committed.node_ids[0],
            canonical_raw_bytes=projection.canonical_raw_bytes,
            raw_sha256=projection.raw_sha256,
            projection_revision=projection.projection_revision,
            expected_artifact_id=projection.spill_artifact_id,
        )

        def fail(stage: str) -> None:
            if stage == "outcome_after_result":
                raise RuntimeError("injected")

        repository = SQLiteAgentRepository(
            self.session_factory,
            fault_injector=fail,
        )
        with self.assertRaisesRegex(RuntimeError, "injected"):
            await repository.commit_agent_call_outcome(
                AgentCallOutcomeCommit(
                    "run-1",
                    committed.run.revision,
                    None,
                    call.item_id,
                    projection.safe_result_payload,
                    AgentCallOutcomeStatus.COMPLETED,
                    staged_artifacts=(staged,),
                )
            )

        with self.session_factory() as session:
            self.assertIsNone(session.get(ArtifactRow, staged.artifact_id))
            node = session.get(TaskNodeRow, committed.node_ids[0])
            self.assertEqual(node.status, "pending")
        metadata = parse_skill_result_storage_ref(staged.storage_ref)
        self.assertTrue(file_store.open_path(str(metadata["storage_key"])).exists())
        self.assertEqual(len(list(manifest_root.iterdir())), 1)

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
