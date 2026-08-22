from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

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
    AgentToolCall,
    AgentUsage,
)
from src.storage.runtime_sidecar_agent_repository import RuntimeSidecarAgentRepository
from src.storage.agent_payload import agent_compaction_source_digest
from tests.integrations.test_runtime_sidecar_grpc_client import (
    _connect_with_retry,
    _ensure_runtime_sidecar_binary,
    _free_loopback_port,
    _repo_root,
    _terminate_process,
)


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
        self.task = {
            "task_id": "task-agent-repository",
            "conversation_id": "conv-agent-repository",
            "root_message_id": "message-agent-repository",
            "status": "accepted",
            "routing_mode": "auto",
            "requested_capability_id": None,
            "root_node_id": None,
            "summary": None,
            "cancel_requested_at": None,
            "created_at": "2026-08-22T11:59:00+00:00",
            "updated_at": None,
            "assignment": None,
        }
        self.client.submit_task(
            task_id=self.task["task_id"],
            conversation_id=self.task["conversation_id"],
            task=self.task,
            idempotency_key="agent-repository-task",
        )

    async def asyncTearDown(self) -> None:
        _terminate_process(self._process)
        self._temp_dir.cleanup()
        await super().asyncTearDown()

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
