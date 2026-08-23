from __future__ import annotations

import inspect
import json
import unittest

from src.api.agent_projection import AgentEventProjector, AgentTaskProjectionService
from src.api.runtime import build_api_runtime
from src.api.sse import InMemoryEventBroker, publish_agent_reasoning_delta
from src.core.enums import EventVisibility, MessageRole, TaskStatus
from src.core.models import Conversation, EventRecord, Message, Task
from src.orchestration.capability_fallback import (
    CAPABILITY_MISSING_FALLBACK_EVENT,
    build_capability_missing_fallback_metadata,
    ensure_fallback_disclosure,
)
from src.orchestration.agent_loop.models import (
    AgentCallOutcomeCommit,
    AgentCallOutcomeStatus,
    AgentFinalOutputCommit,
    AgentFinishMetadata,
    AgentModelBinding,
    AgentRun,
    AgentRunStatus,
    AgentSample,
    AgentSampleCommit,
    AgentToolCall,
    AgentUsage,
)
from src.storage.sqlite import SQLiteAgentRepository
from tests.api.support import APITestCase


class _RecordingAuditSink:
    def __init__(self) -> None:
        self.records = []

    async def record(self, *args, **kwargs) -> None:
        self.records.append((args, kwargs))


class AgentTaskProjectionAPITest(APITestCase):
    async def _seed_task(self, suffix: str):
        conversation_id = f"conv-agent-{suffix}"
        task_id = f"task-agent-{suffix}"
        message_id = f"message-agent-{suffix}"
        await self.runtime.storage.save_conversation(
            Conversation(conversation_id=conversation_id, username="acc-1")
        )
        await self.runtime.storage.save_message(
            Message(
                message_id=message_id,
                conversation_id=conversation_id,
                role=MessageRole.USER,
                content="agent fixture",
                task_id=task_id,
            )
        )
        await self.runtime.storage.save_task(
            Task(
                task_id=task_id,
                conversation_id=conversation_id,
                root_message_id=message_id,
                status=TaskStatus.RUNNING,
            )
        )
        repository = SQLiteAgentRepository(self.runtime.storage._session_factory)
        binding = AgentModelBinding("edition-agent")
        run = await repository.create_run(
            AgentRun(
                f"run-agent-{suffix}",
                task_id,
                conversation_id,
                AgentRunStatus.RUNNING,
                binding,
            )
        )
        self.runtime.agent_task_projection = AgentTaskProjectionService(
            runs=repository,
            tasks=self.runtime.storage,
        )
        return repository, run, binding

    async def test_waiting_run_projects_running_task_and_empty_edge_ledger(self) -> None:
        repository, run, binding = await self._seed_task("waiting")
        sampled = await repository.commit_agent_sample(
            AgentSampleCommit(
                run.run_id,
                run.revision,
                None,
                AgentSample(
                    "sample-waiting",
                    binding,
                    "",
                    (AgentToolCall("call-waiting", "tool_safe", "{}", 0),),
                    AgentUsage(status="usage_unavailable"),
                    AgentFinishMetadata("tool_calls", 1),
                ),
                {"tool_safe": "skill.safe"},
            )
        )
        await repository.commit_agent_call_outcome(
            AgentCallOutcomeCommit(
                run.run_id,
                sampled.run.revision,
                None,
                sampled.call_items[0].item_id,
                {"status": "waiting"},
                AgentCallOutcomeStatus.WAITING_FOR_INPUT,
            )
        )
        edge_reads = 0

        async def forbidden_edge_read(_task_id):
            nonlocal edge_reads
            edge_reads += 1
            raise AssertionError("Agent graph projection must not read TaskEdge")

        self.runtime.storage.list_task_edges = forbidden_edge_read  # type: ignore[method-assign]

        task_response = await self.client.get(f"/api/v1/tasks/{run.task_id}")
        graph_response = await self.client.get(f"/api/v1/tasks/{run.task_id}/graph")

        self.assertEqual(task_response.status_code, 200)
        self.assertEqual(task_response.json()["status"], "running")
        self.assertEqual(task_response.json()["active_node_count"], 1)
        self.assertEqual(graph_response.status_code, 200)
        graph = graph_response.json()
        self.assertEqual(graph["edges"], [])
        self.assertEqual(edge_reads, 0)
        self.assertEqual(
            {(node["criticality"], node["dependency_type"]) for node in graph["nodes"]},
            {("required", "hard")},
        )
        self.assertEqual(graph["nodes"][0]["status"], "waiting_for_input")

    async def test_final_history_is_live_refresh_stable_and_not_duplicated(self) -> None:
        repository, run, binding = await self._seed_task("final")
        fallback = build_capability_missing_fallback_metadata(
            reason_code="skill_missing",
            missing_capability_summary="目标Skill当前不可用",
        )
        final_text = ensure_fallback_disclosure("Agent最终答案", fallback)
        candidate = await repository.commit_agent_sample(
            AgentSampleCommit(
                run.run_id,
                run.revision,
                None,
                AgentSample(
                    "sample-final",
                    binding,
                    final_text,
                    (),
                    AgentUsage(status="usage_unavailable"),
                    AgentFinishMetadata("stop", 1),
                ),
                {},
            )
        )
        final = await repository.commit_agent_final_output(
            AgentFinalOutputCommit(
                run.run_id,
                candidate.run.revision,
                None,
                final_text,
            )
        )
        await self.runtime.storage.append_event(
            EventRecord(
                event_id="event-agent-fallback",
                conversation_id=run.conversation_id,
                task_id=run.task_id,
                event_type=CAPABILITY_MISSING_FALLBACK_EVENT,
                payload=fallback,
                visibility=EventVisibility.FRONTEND,
            )
        )

        first = await self.client.get(
            f"/api/v1/conversations/{run.conversation_id}/messages"
        )
        second = await self.client.get(
            f"/api/v1/conversations/{run.conversation_id}/messages"
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json(), second.json())
        assistants = [
            message
            for message in first.json()["messages"]
            if message["role"] == "assistant"
        ]
        self.assertEqual(len(assistants), 1)
        self.assertEqual(assistants[0]["message_id"], final.message_id)
        self.assertEqual(assistants[0]["content"], final_text)
        self.assertEqual(assistants[0]["stream_status"], "complete")
        self.assertEqual(assistants[0]["artifacts"], [])
        self.assertEqual(
            assistants[0]["metadata"]["capability_missing_fallback"]["reason_code"],
            "skill_missing",
        )
        stored = await self.runtime.storage.list_messages_for_conversation(
            run.conversation_id
        )
        sidecar_like_base = [
            message for message in stored if str(message.role) != "assistant"
        ]
        projected = await self.runtime.agent_task_projection.project_history_messages(
            run.conversation_id,
            sidecar_like_base,
        )
        projected_assistant = next(
            message for message in projected if str(message.role) == "assistant"
        )
        self.assertEqual(projected_assistant.content, final_text)
        self.assertEqual(projected_assistant.stream_status, "complete")

    async def test_status_transition_does_not_break_read_projection(self) -> None:
        _repository, run, _binding = await self._seed_task("mismatch")
        task = await self.runtime.storage.get_task(run.task_id)
        await self.runtime.storage.save_task(
            Task(
                task_id=task.task_id,
                conversation_id=task.conversation_id,
                root_message_id=task.root_message_id,
                status=TaskStatus.COMPLETED,
            )
        )

        projected = await self.runtime.agent_task_projection.get_agent_run(run.task_id)
        self.assertEqual(projected, run)

    async def test_agent_frontend_event_replays_and_real_runtime_assembles_projection(self) -> None:
        _repository, run, _binding = await self._seed_task("event")
        event = AgentEventProjector().durable(
            event_id="event-agent-waiting",
            conversation_id=run.conversation_id,
            task_id=run.task_id,
            event_type="agent.run.waiting",
            payload={
                "interrupt_id": "interrupt-agent-1",
                "reason_kind": "skill_input",
                "remaining_count": 1,
            },
        )
        await self.runtime.storage.append_event(event)

        iterator = self.runtime.iter_frontend_events(run.task_id).__aiter__()
        replayed = await iterator.__anext__()
        await iterator.aclose()

        self.assertEqual(replayed, event)
        runtime_source = inspect.getsource(build_api_runtime)
        self.assertIn("AgentTaskProjectionService", runtime_source)
        self.assertIn("agent_task_projection", runtime_source)
        self.assertNotIn("OrchestrationService", runtime_source)

    async def test_agent_run_initialization_projects_empty_graph_created_event(self) -> None:
        _repository, run, _binding = await self._seed_task("graph-created")
        event = AgentEventProjector().graph_created(
            event_id="event-agent-graph-created",
            conversation_id=run.conversation_id,
            task_id=run.task_id,
        )
        await self.runtime.storage.append_event(event)

        iterator = self.runtime.iter_frontend_events(run.task_id).__aiter__()
        replayed = await iterator.__anext__()
        await iterator.aclose()

        self.assertEqual(replayed, event)
        self.assertEqual(replayed.event_type, "task.graph_created")
        self.assertEqual(
            replayed.payload,
            {"edge_count": 0, "node_count": 0, "root_node_id": None},
        )


class AgentEventProjectionTest(unittest.IsolatedAsyncioTestCase):
    async def test_durable_waiting_event_is_closed_replayable_and_leak_free(self) -> None:
        event = AgentEventProjector().durable(
            event_id="event-waiting",
            conversation_id="conv-1",
            task_id="task-1",
            node_id="node-1",
            event_type="agent.run.waiting",
            payload={
                "interrupt_id": "interrupt-1",
                "reason_kind": "mcp_approval",
                "remaining_count": 2,
            },
        )

        self.assertEqual(event.visibility, EventVisibility.FRONTEND)
        serialized = json.dumps(event.payload)
        for forbidden in ("credential", "raw_result", "arguments", "user text"):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual(event.payload["remaining_count"], 2)

    async def test_reasoning_delta_is_transient_and_bypasses_audit(self) -> None:
        audit = _RecordingAuditSink()
        broker = InMemoryEventBroker(audit_sink=audit)
        subscription = broker.subscribe("task-1")
        event = AgentEventProjector().reasoning_delta(
            event_id="reasoning-1",
            conversation_id="conv-1",
            task_id="task-1",
            sample_id="sample-1",
            ordinal=0,
            delta="仅瞬时展示",
        )

        await publish_agent_reasoning_delta(broker, event)
        received = await subscription.get()

        self.assertEqual(received, event)
        self.assertEqual(audit.records, [])
        subscription.close()

    async def test_unknown_or_unsafe_durable_event_fails_closed(self) -> None:
        projector = AgentEventProjector()
        with self.assertRaisesRegex(ValueError, "contract_invalid"):
            projector.durable(
                event_id="event-1",
                conversation_id="conv-1",
                task_id="task-1",
                event_type="agent.reasoning_delta",
                payload={"delta": "must stay transient"},
            )
        with self.assertRaisesRegex(ValueError, "contract_invalid"):
            projector.durable(
                event_id="event-2",
                conversation_id="conv-1",
                task_id="task-1",
                event_type="agent.run.waiting",
                payload={
                    "interrupt_id": "interrupt-1",
                    "reason_kind": "skill_input",
                    "remaining_count": 1,
                    "raw_result": "secret",
                },
            )
        with self.assertRaisesRegex(ValueError, "payload_unsafe"):
            projector.durable(
                event_id="event-3",
                conversation_id="conv-1",
                task_id="task-1",
                event_type="agent.run.started",
                payload={
                    "model_option_digests": {"credential": "d" * 64},
                    "routing_mode": "auto",
                },
            )
