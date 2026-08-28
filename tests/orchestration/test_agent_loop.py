from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from src.orchestration.agent_loop.context import AgentContextBuilder, AgentContextRules
from src.orchestration.agent_loop.lease import AgentLeaseController
from src.orchestration.agent_loop.models import (
    AgentCallOutcomeStatus,
    AgentFinishMetadata,
    AgentModelBinding,
    AgentRun,
    AgentRunStatus,
    AgentSample,
    AgentToolCall,
    AgentUsage,
)
from src.orchestration.agent_loop.runner import (
    AGENT_REASONING_TRUNCATED_MARKER,
    MAX_AGENT_REASONING_BYTES,
    AgentCallExecution,
    AgentLoopRunner,
)
from src.orchestration.agent_loop.tool_catalog import (
    AgentToolCatalogBuilder,
    CapabilityInvocationPolicy,
    CapabilityVisibilityContext,
)
from src.orchestration.models import CapabilityDescriptor
from src.orchestration.registry import CapabilityRegistry
from src.storage.sqlite import (
    SQLiteAgentRepository,
    bootstrap_sqlite_database,
    create_sqlite_engine,
    create_sqlite_session_factory,
)
from src.storage.sqlite.models import TaskRow


def _policy(*, parallel_safe: bool) -> CapabilityInvocationPolicy:
    return CapabilityInvocationPolicy(
        model_allowed_fields=(),
        input_schema={"type": "object", "additionalProperties": False},
        parallel_safe=parallel_safe,
    )


class _QueuedModel:
    def __init__(
        self,
        binding: AgentModelBinding,
        outputs,
        *,
        trace: list[str] | None = None,
    ) -> None:
        self.binding = binding
        self.outputs = iter(outputs)
        self.requests = []
        self.trace = trace

    async def sample_agent(self, request):
        if self.trace is not None:
            self.trace.append("model.sample")
        self.requests.append(request)
        text, calls = next(self.outputs)
        return AgentSample(
            sample_id=f"sample-{len(self.requests)}",
            binding=self.binding,
            visible_text=text,
            tool_calls=tuple(calls),
            usage=AgentUsage(status="usage_unavailable"),
            finish=AgentFinishMetadata(
                finish_reason="tool_calls" if calls else "stop",
                attempts=1,
                mixed_text_and_tool_calls=bool(text and calls),
            ),
        )


class _ReasoningModel:
    def __init__(self, binding: AgentModelBinding, actions: list[tuple[str, str | None]]) -> None:
        self.binding = binding
        self.actions = actions

    async def sample_agent(self, request):
        for action, value in self.actions:
            if action == "delta":
                await request.reasoning_delta_sink(value or "")
            elif action == "reset":
                await request.reasoning_reset_sink()
        return AgentSample(
            sample_id="reasoning-sample",
            binding=self.binding,
            visible_text="final answer",
            tool_calls=(),
            usage=AgentUsage(status="usage_unavailable"),
            finish=AgentFinishMetadata("stop", 1),
        )


class _RecordingInvoker:
    def __init__(self, outcomes=None, *, trace: list[str] | None = None) -> None:
        self.events = []
        self.lease_handles = []
        self.outcomes = dict(outcomes or {})
        self.trace = trace

    async def invoke(self, *, call, effective_payload, lease_handle, **_kwargs):
        if self.trace is not None:
            self.trace.append("capability.invoke")
        self.lease_handles.append(lease_handle)
        self.events.append(f"start:{call.call_id}")
        if call.call_id == "slow":
            await asyncio.sleep(0.02)
        else:
            await asyncio.sleep(0)
        self.events.append(f"end:{call.call_id}")
        return self.outcomes.get(
            call.call_id,
            AgentCallExecution(
                AgentCallOutcomeStatus.COMPLETED,
                safe_result_payload={"call_id": call.call_id},
            ),
        )


class _LogicalTraceRepository:
    _RECORDED = frozenset(
        {
            "acquire_task_lease",
            "commit_agent_call_outcome",
            "commit_agent_sample",
            "release_waiting_task_lease",
            "renew_task_lease",
        }
    )

    def __init__(self, repository, trace: list[str]) -> None:
        self._repository = repository
        self._trace = trace

    def __getattr__(self, name: str):
        attribute = getattr(self._repository, name)
        if name not in self._RECORDED:
            return attribute

        async def traced(*args, **kwargs):
            self._trace.append(f"repository.{name}")
            return await attribute(*args, **kwargs)

        return traced


class AgentLoopRunnerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.engine = create_sqlite_engine(Path(self.temp_dir.name) / "loop.sqlite")
        self.sessions = create_sqlite_session_factory(self.engine)
        bootstrap_sqlite_database(self.engine)
        with self.sessions.begin() as session:
            session.add(
                TaskRow(
                    task_id="task-1",
                    conversation_id="conv-1",
                    root_message_id="message-1",
                    status="running",
                    routing_mode="auto",
                )
            )
        self.repository = SQLiteAgentRepository(self.sessions)
        self.binding = AgentModelBinding("edition-a")
        await self.repository.create_run(
            AgentRun(
                "run-1",
                "task-1",
                "conv-1",
                AgentRunStatus.RUNNING,
                self.binding,
            )
        )
        registry = CapabilityRegistry()
        for capability_id, parallel in (
            ("skill.safe_one", True),
            ("skill.safe_two", True),
            ("skill.exclusive", False),
        ):
            registry.register(
                CapabilityDescriptor(
                    capability_id,
                    capability_id,
                    capability_id,
                    kind="skill",
                    source="skill",
                ),
                invocation_policy=_policy(parallel_safe=parallel),
            )
        self.catalog_builder = AgentToolCatalogBuilder(registry)
        self.visibility = CapabilityVisibilityContext("owner")
        catalog = self.catalog_builder.build(self.visibility)
        self.names = {
            tool.capability_id: tool.provider_safe_name for tool in catalog.tools
        }

    async def asyncTearDown(self) -> None:
        self.engine.dispose()
        self.temp_dir.cleanup()
        await super().asyncTearDown()

    def _runner(
        self,
        model,
        invoker,
        *,
        repository=None,
        reasoning_delta_sink=None,
        reasoning_reset_sink=None,
        terminal_event_recorder=None,
    ):
        repository = repository or self.repository
        return AgentLoopRunner(
            runs=repository,
            writer=repository,
            model=model,
            context_builder=AgentContextBuilder(
                AgentContextRules("stable", "tool rules", "final guard")
            ),
            catalog_builder=self.catalog_builder,
            visibility_context=self.visibility,
            lease_controller=AgentLeaseController(repository, ttl_seconds=30),
            invoker=invoker,
            owner_id="worker-1",
            reasoning_delta_sink=reasoning_delta_sink,
            reasoning_reset_sink=reasoning_reset_sink,
            terminal_event_recorder=terminal_event_recorder,
        )

    async def test_reasoning_reset_keeps_ordinal_monotonic_and_budget_consumed(self) -> None:
        deltas: list[tuple[str, int]] = []
        resets: list[tuple[str, int]] = []

        async def record_delta(_run, delta: str, ordinal: int) -> None:
            deltas.append((delta, ordinal))

        async def record_reset(_run, sample_id: str, ordinal: int) -> None:
            resets.append((sample_id, ordinal))

        content_limit = MAX_AGENT_REASONING_BYTES - len(
            AGENT_REASONING_TRUNCATED_MARKER.encode("utf-8")
        )
        model = _ReasoningModel(
            self.binding,
            [
                ("delta", "a" * (content_limit - 1)),
                ("reset", None),
                ("delta", "界"),
                ("reset", None),
                ("delta", "ignored"),
            ],
        )
        result = await self._runner(
            model,
            _RecordingInvoker(),
            reasoning_delta_sink=record_delta,
            reasoning_reset_sink=record_reset,
        ).run("run-1")

        self.assertEqual(result.state, "final_candidate")
        self.assertEqual(
            deltas,
            [
                ("a" * (content_limit - 1), 1),
                (AGENT_REASONING_TRUNCATED_MARKER, 2),
            ],
        )
        self.assertEqual(
            resets,
            [
                ("agent-sample:run-1:r1", 1),
                ("agent-sample:run-1:r1", 2),
            ],
        )
        published_bytes = sum(len(delta.encode("utf-8")) for delta, _ in deltas)
        self.assertLessEqual(published_bytes, MAX_AGENT_REASONING_BYTES)

    async def test_reasoning_utf8_boundary_preserves_complete_codepoints(self) -> None:
        deltas: list[str] = []

        async def record_delta(_run, delta: str, _ordinal: int) -> None:
            deltas.append(delta)

        content_limit = MAX_AGENT_REASONING_BYTES - len(
            AGENT_REASONING_TRUNCATED_MARKER.encode("utf-8")
        )
        model = _ReasoningModel(
            self.binding,
            [("delta", "a" * (content_limit - 2) + "界")],
        )
        result = await self._runner(
            model,
            _RecordingInvoker(),
            reasoning_delta_sink=record_delta,
        ).run("run-1")

        self.assertEqual(result.state, "final_candidate")
        self.assertEqual(deltas[-1], AGENT_REASONING_TRUNCATED_MARKER)
        self.assertTrue(deltas[0].endswith("a"))
        self.assertNotIn("�", deltas[0])

    async def test_multi_step_loop_orders_outcomes_and_serializes_exclusive_wave(self) -> None:
        calls = (
            AgentToolCall("slow", self.names["skill.safe_one"], "{}", 0),
            AgentToolCall("fast", self.names["skill.safe_two"], "{}", 1),
            AgentToolCall("exclusive", self.names["skill.exclusive"], "{}", 2),
        )
        failed = AgentToolCall("ordinary-failure", self.names["skill.safe_one"], "{}", 0)
        model = _QueuedModel(
            self.binding,
            [
                ("ignored mixed text", calls),
                ("", (failed,)),
                ("final answer", ()),
            ],
        )
        invoker = _RecordingInvoker(
            {
                "ordinary-failure": AgentCallExecution(
                    AgentCallOutcomeStatus.FAILED,
                    safe_error_code="ordinary_error",
                )
            }
        )

        result = await self._runner(model, invoker).run(
            "run-1",
            initial_required_tool_name=self.names["skill.safe_one"],
        )

        self.assertEqual(result.state, "final_candidate")
        self.assertEqual(result.final_candidate.payload_json.find("final answer") > 0, True)
        self.assertEqual(model.requests[0].tool_choice.mode, "required")
        self.assertEqual(model.requests[1].tool_choice.mode, "auto")
        self.assertGreater(len([m for m in model.requests[1].messages if m.role == "tool"]), 0)
        self.assertLess(invoker.events.index("end:slow"), invoker.events.index("start:exclusive"))
        self.assertLess(invoker.events.index("end:fast"), invoker.events.index("start:exclusive"))
        self.assertTrue(invoker.lease_handles)
        self.assertEqual(
            len({id(handle) for handle in invoker.lease_handles}),
            1,
        )
        items = await self.repository.list_items("run-1")
        committed_results = [item for item in items if item.kind.value == "tool_result"]
        self.assertEqual([item.call_ordinal for item in committed_results], [0, 1, 2, 3])

    async def test_outcome_response_loss_replays_exact_and_publishes_one_terminal_event(self) -> None:
        call = AgentToolCall(
            "response-lost",
            self.names["skill.safe_one"],
            "{}",
            0,
        )
        model = _QueuedModel(
            self.binding,
            [("", (call,)), ("final answer", ())],
        )
        events = []

        async def record_terminal(event) -> None:
            events.append(event)

        class ResponseLostRepository:
            def __init__(self, repository) -> None:
                self.repository = repository
                self.lost = False

            def __getattr__(self, name):
                return getattr(self.repository, name)

            async def commit_agent_call_outcome(self, commit):
                result = await self.repository.commit_agent_call_outcome(commit)
                if not self.lost:
                    self.lost = True
                    raise RuntimeError("response lost")
                return result

        repository = ResponseLostRepository(self.repository)
        result = await self._runner(
            model,
            _RecordingInvoker(),
            repository=repository,
            terminal_event_recorder=record_terminal,
        ).run("run-1")

        self.assertEqual(result.state, "final_candidate")
        items = await self.repository.list_items("run-1")
        committed_results = [
            item
            for item in items
            if item.kind.value == "tool_result" and item.state.value == "committed"
        ]
        self.assertEqual(len(committed_results), 1)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event_type, "node.completed")
        self.assertEqual(
            events[0].payload["result_sha256"],
            committed_results[0].payload_sha256,
        )

    async def test_waiting_stops_before_another_model_sample_and_releases_lease(self) -> None:
        waiting_call = AgentToolCall(
            "waiting",
            self.names["skill.exclusive"],
            "{}",
            0,
        )
        model = _QueuedModel(self.binding, [("", (waiting_call,))])
        invoker = _RecordingInvoker(
            {
                "waiting": AgentCallExecution(
                    AgentCallOutcomeStatus.WAITING_FOR_INPUT,
                    safe_result_payload={"question": "need input"},
                )
            }
        )
        result = await self._runner(model, invoker).run("run-1")
        self.assertEqual(result.state, "waiting")
        self.assertEqual(len(model.requests), 1)
        self.assertIsNone(result.run.claim_token)
        self.assertEqual(result.run.status, AgentRunStatus.WAITING_FOR_INPUT)

    async def test_waiting_logical_call_sites_have_exact_order_and_counts(self) -> None:
        trace: list[str] = []
        waiting_call = AgentToolCall(
            "waiting-trace",
            self.names["skill.exclusive"],
            "{}",
            0,
        )
        model = _QueuedModel(
            self.binding,
            [("", (waiting_call,))],
            trace=trace,
        )
        invoker = _RecordingInvoker(
            {
                "waiting-trace": AgentCallExecution(
                    AgentCallOutcomeStatus.WAITING_FOR_INPUT,
                    safe_result_payload={"question": "need input"},
                )
            },
            trace=trace,
        )
        repository = _LogicalTraceRepository(self.repository, trace)

        result = await self._runner(
            model,
            invoker,
            repository=repository,
        ).run("run-1")

        self.assertEqual(result.state, "waiting")
        self.assertEqual(
            trace,
            [
                "repository.acquire_task_lease",
                "model.sample",
                "repository.commit_agent_sample",
                "capability.invoke",
                "repository.commit_agent_call_outcome",
                "repository.release_waiting_task_lease",
            ],
        )
        self.assertEqual(len(model.requests), 1)
        self.assertEqual(invoker.events, ["start:waiting-trace", "end:waiting-trace"])
        self.assertIsNone(result.run.claim_token)
