from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from src.core.enums import TaskStatus
from src.core.models import Task
from src.orchestration.agent_loop.models import (
    AgentCallOutcomeCommit,
    AgentCallOutcomeStatus,
    AgentCompactionCommit,
    AgentFinishMetadata,
    AgentFinalOutputCommit,
    AgentItemState,
    AgentModelBinding,
    AgentRun,
    AgentRunStatus,
    AgentSample,
    AgentSampleCommit,
    AgentStagedArtifact,
    AgentStorageConflict,
    AgentToolCall,
    AgentUsage,
    AgentUserMessageCommit,
)
from src.orchestration.agent_loop.context_budget import AgentContextBudget
from src.orchestration.agent_loop.result_artifacts import (
    AgentSkillResultArtifactStager,
)
from src.orchestration.agent_loop.result_projection import (
    SKILL_RESULT_PROJECTION_POLICY_FULL_INLINE_THEN_TRANSIENT,
)
from src.storage.artifact_files import LocalArtifactFileStore
from src.storage.runtime_sidecar_agent_repository import RuntimeSidecarAgentRepository
from src.storage.agent_payload import agent_compaction_source_digest
from tests.integrations.test_runtime_sidecar_grpc_client import (
    _connect_with_retry,
    _ensure_runtime_sidecar_binary,
    _free_loopback_port,
    _repo_root,
    _terminate_process,
)
from tests.orchestration.support import make_agent_result_projector


class RuntimeSidecarAgentRepositoryIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self._temp_dir = tempfile.TemporaryDirectory()
        port = _free_loopback_port()
        endpoint = f"http://127.0.0.1:{port}"
        self._process = subprocess.Popen(
            [
                str(_ensure_runtime_sidecar_binary()),
                "--serve",
                f"127.0.0.1:{port}",
                "--sqlite",
                str(Path(self._temp_dir.name) / "sidecar.sqlite"),
            ],
            cwd=_repo_root(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.client = _connect_with_retry(endpoint, process=self._process)
        self.now = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
        self.repository = RuntimeSidecarAgentRepository(self.client, now_fn=lambda: self.now)
        self.task = self._submit_task(
            "task-agent-repository",
            "conv-agent-repository",
            "agent-repository-task",
        )

    def _submit_task(
        self,
        task_id: str,
        conversation_id: str,
        idempotency_key: str,
        *,
        status: str = "accepted",
    ) -> dict[str, object]:
        task: dict[str, object] = {
            "task_id": task_id,
            "conversation_id": conversation_id,
            "root_message_id": "message-agent-repository",
            "status": status,
            "routing_mode": "auto",
            "requested_capability_id": None,
            "summary": None,
            "cancel_requested_at": None,
            "created_at": "2026-08-22T11:59:00+00:00",
            "updated_at": None,
            "assignment": None,
        }
        self.client.submit_task(
            task_id=task_id,
            conversation_id=conversation_id,
            task=task,
            idempotency_key=idempotency_key,
        )
        return task

    async def asyncTearDown(self) -> None:
        _terminate_process(self._process)
        self._temp_dir.cleanup()
        await super().asyncTearDown()

    async def test_create_terminal_run_reuses_existing_commit_agent_state_wire(self) -> None:
        task = Task(
            task_id=str(self.task["task_id"]),
            conversation_id=str(self.task["conversation_id"]),
            root_message_id=str(self.task["root_message_id"]),
            status=TaskStatus.ACCEPTED,
        )
        expected = AgentRun(
            run_id=f"agent-run:{task.task_id}",
            task_id=task.task_id,
            conversation_id=task.conversation_id,
            status=AgentRunStatus.FAILED,
            binding=AgentModelBinding("edition-terminal"),
            terminal_reason_code="agent_skill_bundle_revision_retired",
        )

        created = await self.repository.create_terminal_run(expected, task=task)
        replayed = await self.repository.create_terminal_run(
            expected,
            task=Task(
                task_id=task.task_id,
                conversation_id=task.conversation_id,
                root_message_id=task.root_message_id,
                status=TaskStatus.FAILED,
            ),
        )

        self.assertEqual(replayed, created)
        self.assertEqual(created.status, AgentRunStatus.FAILED)
        self.assertEqual(await self.repository.list_items(created.run_id), ())
        stored_task = self.client.get_task(task_id=task.task_id)
        self.assertEqual(stored_task["task"]["status"], "failed")

    async def test_sample_outcomes_and_final_projection_are_one_sidecar_authority(self) -> None:
        binding = AgentModelBinding(
            "edition-a", reasoning_effort="high", option_digests={"policy": "abc"}
        )
        run = await self.repository.create_run(
            AgentRun(
                run_id="run-agent-repository",
                task_id=self.task["task_id"],
                conversation_id=self.task["conversation_id"],
                status=AgentRunStatus.RUNNING,
                binding=binding,
            )
        )
        self.assertEqual(
            await self.repository.get_run_for_task(self.task["task_id"]),
            run,
        )
        sample = AgentSample(
            sample_id="sample-1",
            binding=binding,
            visible_text="",
            tool_calls=(
                AgentToolCall("call-1", "tool_one", "{}", 0),
                AgentToolCall("call-2", "tool_two", '{"x":1}', 1),
            ),
            usage=AgentUsage(status="usage_unavailable"),
            finish=AgentFinishMetadata(finish_reason="tool_calls", attempts=1),
        )
        sampled = await self.repository.commit_agent_sample(
            AgentSampleCommit(
                run.run_id,
                run.revision,
                None,
                sample,
                {"tool_one": "skill.one", "tool_two": "mcp.dispatch"},
            )
        )
        self.assertEqual(
            [item.state for item in sampled.result_reservations],
            [AgentItemState.RESERVED, AgentItemState.RESERVED],
        )
        self.assertEqual(
            len(self.client.list_task_nodes_for_task(task_id=run.task_id)["nodes"]),
            2,
        )

        first = await self.repository.commit_agent_call_outcome(
            AgentCallOutcomeCommit(
                run.run_id,
                sampled.run.revision,
                None,
                sampled.call_items[0].item_id,
                {"ok": True},
                AgentCallOutcomeStatus.COMPLETED,
                (AgentStagedArtifact("artifact-call-1", "json", "opaque://call-1"),),
            )
        )
        self.assertEqual(first.state, AgentItemState.COMMITTED)
        after_first = await self.repository.get_run(run.run_id)
        waiting = await self.repository.commit_agent_call_outcome(
            AgentCallOutcomeCommit(
                run.run_id,
                after_first.revision,
                None,
                sampled.call_items[1].item_id,
                {"prompt": "need input"},
                AgentCallOutcomeStatus.WAITING_FOR_INPUT,
                (AgentStagedArtifact("artifact-waiting", "json", "opaque://waiting"),),
            )
        )
        self.assertEqual(waiting.state, AgentItemState.RESERVED)
        waiting_run = await self.repository.get_run(run.run_id)
        self.assertEqual(waiting_run.status, AgentRunStatus.WAITING_FOR_INPUT)
        self.assertIsNone(
            self.client.get_artifact(artifact_id="artifact-waiting")["artifact"]
        )
        second = await self.repository.commit_agent_call_outcome(
            AgentCallOutcomeCommit(
                run.run_id,
                waiting_run.revision,
                None,
                sampled.call_items[1].item_id,
                {"ok": True},
                AgentCallOutcomeStatus.COMPLETED,
                (AgentStagedArtifact("artifact-waiting", "json", "opaque://waiting"),),
                continuation_payload={
                    "authority_digest": "d" * 64,
                    "schema": "maf.agent.continuation.v1",
                },
            )
        )
        self.assertEqual(second.state, AgentItemState.COMMITTED)
        after_second = await self.repository.get_run(run.run_id)
        self.assertEqual(after_second.status, AgentRunStatus.RUNNING)
        covered = tuple(
            item
            for item in await self.repository.list_items(run.run_id)
            if 1 <= item.sequence <= 6
        )
        self.assertEqual(covered[-1].kind.value, "continuation")
        compacted = await self.repository.commit_agent_compaction(
            AgentCompactionCommit(
                run.run_id,
                after_second.revision,
                None,
                1,
                6,
                agent_compaction_source_digest(covered),
                "safe compacted facts",
            )
        )
        self.assertEqual(compacted.run.compacted_through_sequence, 6)
        after_second = compacted.run
        final = await self.repository.commit_agent_final_output(
            AgentFinalOutputCommit(run.run_id, after_second.revision, None, "最终答案")
        )
        self.assertEqual(final.run.status, AgentRunStatus.COMPLETED)
        self.assertEqual(
            self.client.get_task(task_id=run.task_id)["task"]["status"],
            "completed",
        )
        self.assertIsNotNone(
            self.client.get_artifact(artifact_id=final.artifact_id)["artifact"]
        )
        projection = self.client.get_agent_final_projection(run_id=run.run_id)
        self.assertTrue(projection["found"])
        final_projection = json.loads(projection["projection_json"])
        self.assertEqual(set(final_projection), {"event", "message", "receipt"})
        self.assertEqual(final_projection["message"]["message_id"], final.message_id)
        self.assertEqual(final_projection["event"]["event_id"], final.event_id)
        self.assertEqual(final_projection["receipt"]["receipt_id"], final.receipt_id)
        retry = await self.repository.commit_agent_final_output(
            AgentFinalOutputCommit(run.run_id, after_second.revision, None, "最终答案")
        )
        self.assertEqual(retry, final)

    async def test_recovery_user_message_and_terminal_operations_preserve_current_sidecar_traces(
        self,
    ) -> None:
        binding = AgentModelBinding(
            "edition-a", reasoning_effort="high", option_digests={"policy": "abc"}
        )
        running = await self.repository.create_run(
            AgentRun(
                run_id="run-agent-supported",
                task_id=self.task["task_id"],
                conversation_id=self.task["conversation_id"],
                status=AgentRunStatus.RUNNING,
                binding=binding,
            )
        )
        committed = await self.repository.commit_agent_user_message(
            AgentUserMessageCommit(
                running.run_id,
                running.revision,
                None,
                "initial user message",
                context_budget=AgentContextBudget.from_model_context_window(
                    450_000
                ),
            )
        )
        self.assertEqual(committed.item.kind.value, "user_message")
        self.assertEqual(
            json.loads(committed.item.payload_json)["context_budget"],
            AgentContextBudget.from_model_context_window(450_000).to_payload(),
        )
        self.assertEqual(
            await self.repository.reconcile_agent_run_consistency(running.run_id),
            committed.run,
        )
        with self.assertRaisesRegex(
            KeyError,
            "Unknown Rust runtime sidecar operation: agent_run_list",
        ):
            await self.repository.list_recoverable_runs()

        failed_task = self._submit_task(
            "task-agent-failed",
            "conv-agent-failed",
            "agent-repository-failed-task",
        )
        failed_run = await self.repository.create_run(
            AgentRun(
                run_id="run-agent-failed",
                task_id=failed_task["task_id"],
                conversation_id=failed_task["conversation_id"],
                status=AgentRunStatus.RUNNING,
                binding=binding,
            )
        )
        failed = await self.repository.fail_agent_run(
            failed_run.run_id,
            expected_revision=failed_run.revision,
            expected_claim_token=None,
            safe_error_code="safe_failure",
        )
        self.assertEqual(failed.status, AgentRunStatus.FAILED)
        self.assertEqual(
            self.client.get_task(task_id=failed.task_id)["task"]["status"],
            "failed",
        )

        cancelled_task = self._submit_task(
            "task-agent-cancelled",
            "conv-agent-cancelled",
            "agent-repository-cancelled-task",
            status="running",
        )
        cancelled_run = await self.repository.create_run(
            AgentRun(
                run_id="run-agent-cancelled",
                task_id=cancelled_task["task_id"],
                conversation_id=cancelled_task["conversation_id"],
                status=AgentRunStatus.RUNNING,
                binding=binding,
            )
        )
        with self.assertRaisesRegex(
            AgentStorageConflict,
            "runtime_store_response_invalid",
        ):
            await self.repository.cancel_agent_run(
                cancelled_run.run_id,
                expected_revision=cancelled_run.revision,
                expected_claim_token=None,
                safe_reason_code="user_cancel",
            )
        self.assertEqual(
            (await self.repository.get_run(cancelled_run.run_id)).status,
            AgentRunStatus.RUNNING,
        )
        self.assertEqual(
            self.client.get_task(task_id=cancelled_run.task_id)["task"]["status"],
            "running",
        )

    async def test_completed_task_convergence_reuses_sidecar_commit_without_projection(
        self,
    ) -> None:
        task = self._submit_task(
            "task-agent-completed-split",
            "conv-agent-completed-split",
            "agent-repository-completed-split-task",
            status="completed",
        )
        run = await self.repository.create_run(
            AgentRun(
                run_id="run-agent-completed-split",
                task_id=task["task_id"],
                conversation_id=task["conversation_id"],
                status=AgentRunStatus.WAITING_FOR_INPUT,
                binding=AgentModelBinding("edition-a"),
                waiting_call_item_ids=("call-pending",),
            )
        )

        completed = await self.repository.complete_agent_run_from_terminal_task(
            run.run_id,
            expected_revision=run.revision,
            expected_claim_token=run.claim_token,
            safe_reason_code="agent_terminal_task_completed_run_convergence",
        )
        replayed = await self.repository.complete_agent_run_from_terminal_task(
            run.run_id,
            expected_revision=run.revision,
            expected_claim_token=run.claim_token,
            safe_reason_code="agent_terminal_task_completed_run_convergence",
        )

        self.assertEqual(replayed, completed)
        self.assertEqual(completed.status, AgentRunStatus.COMPLETED)
        self.assertEqual(completed.waiting_call_item_ids, ())
        self.assertEqual(completed.revision, run.revision + 1)
        self.assertEqual(
            completed.terminal_reason_code,
            "agent_terminal_task_completed_run_convergence",
        )
        self.assertEqual(await self.repository.list_items(run.run_id), ())
        self.assertFalse(
            self.client.get_agent_final_projection(run_id=run.run_id)["found"]
        )
        self.assertEqual(
            self.client.get_task(task_id=run.task_id)["task"]["status"],
            "completed",
        )

    async def test_skill_result_artifact_outcome_replays_exactly_in_sidecar_cas(self) -> None:
        binding = AgentModelBinding("edition-a")
        run = await self.repository.create_run(
            AgentRun(
                "run-agent-skill-result",
                self.task["task_id"],
                self.task["conversation_id"],
                AgentRunStatus.RUNNING,
                binding,
            )
        )
        sampled = await self.repository.commit_agent_sample(
            AgentSampleCommit(
                run.run_id,
                run.revision,
                None,
                AgentSample(
                    "sample-skill-result",
                    binding,
                    "",
                    (AgentToolCall("call-large", "tool_large", "{}", 0),),
                    AgentUsage(status="usage_unavailable"),
                    AgentFinishMetadata("tool_calls", 1),
                ),
                {"tool_large": "skill.large"},
            )
        )
        call = sampled.call_items[0]
        projection = await make_agent_result_projector().project(
            capability_id="skill.large",
            output_payload={"rows": ["x" * 10_000 for _ in range(20)]},
            call_item_id=call.item_id,
            outcome="completed",
            safe_error_code=None,
            model_edition="edition-a",
        )
        staged = AgentSkillResultArtifactStager(
            file_store=LocalArtifactFileStore(
                Path(self._temp_dir.name) / "artifacts"
            ),
            manifest_root=Path(self._temp_dir.name) / "manifests",
        ).stage(
            run=sampled.run,
            call_item=call,
            node_id=sampled.node_ids[0],
            canonical_raw_bytes=projection.spill_content_bytes,
            raw_sha256=projection.spill_content_sha256,
            projection_revision=projection.projection_revision,
            expected_artifact_id=projection.spill_artifact_id,
        )
        commit = AgentCallOutcomeCommit(
            run.run_id,
            sampled.run.revision,
            None,
            call.item_id,
            projection.safe_result_payload,
            AgentCallOutcomeStatus.COMPLETED,
            (staged,),
        )

        first = await self.repository.commit_agent_call_outcome(commit)
        replay = await self.repository.commit_agent_call_outcome(commit)

        self.assertEqual(replay, first)
        artifact = self.client.get_artifact(artifact_id=staged.artifact_id)[
            "artifact"
        ]
        self.assertIsNotNone(artifact)
        self.assertEqual(artifact["storage_ref"], staged.storage_ref)
        node = self.client.get_task_node(node_id=sampled.node_ids[0])["node"]
        self.assertEqual(node["status"], "completed")

    async def test_transient_receipt_replays_without_sidecar_artifact(self) -> None:
        binding = AgentModelBinding("edition-a")
        run = await self.repository.create_run(
            AgentRun(
                "run-agent-transient-result",
                self.task["task_id"],
                self.task["conversation_id"],
                AgentRunStatus.RUNNING,
                binding,
            )
        )
        sampled = await self.repository.commit_agent_sample(
            AgentSampleCommit(
                run.run_id,
                run.revision,
                None,
                AgentSample(
                    "sample-transient-result",
                    binding,
                    "",
                    (AgentToolCall("call-large", "tool_large", "{}", 0),),
                    AgentUsage(status="usage_unavailable"),
                    AgentFinishMetadata("tool_calls", 1),
                ),
                {"tool_large": "skill.large"},
            )
        )
        call = sampled.call_items[0]
        projection = await make_agent_result_projector().project(
            capability_id="skill.large",
            output_payload={"rows": ["x" * 150_000]},
            call_item_id=call.item_id,
            outcome="completed",
            safe_error_code=None,
            skill_projection_policy=(
                SKILL_RESULT_PROJECTION_POLICY_FULL_INLINE_THEN_TRANSIENT
            ),
            model_edition="edition-a",
        )
        commit = AgentCallOutcomeCommit(
            run.run_id,
            sampled.run.revision,
            None,
            call.item_id,
            projection.safe_result_payload,
            AgentCallOutcomeStatus.COMPLETED,
            (),
        )

        first = await self.repository.commit_agent_call_outcome(commit)
        replay = await self.repository.commit_agent_call_outcome(commit)

        self.assertEqual(replay, first)
        self.assertEqual(
            self.client.list_artifacts_for_task(task_id=run.task_id)[
                "artifacts"
            ],
            [],
        )
