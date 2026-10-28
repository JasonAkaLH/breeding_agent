from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from src.core.contracts import CapabilityExecutionResult
from src.core.enums import NodeStatus, TaskStatus
from src.core.models import Task, TaskNode
from src.orchestration.agent_loop.capability_invoker import AgentCapabilityInvoker
from src.orchestration.agent_loop.context import AgentContextBuilder, AgentContextRules
from src.orchestration.agent_loop.context_budget import AgentContextBudget
from src.orchestration.agent_loop.context_preflight import AgentContextCandidateBuilder
from src.orchestration.agent_loop.compaction import AgentCompactionService
from src.orchestration.agent_loop.invocation import InvocationResult
from src.orchestration.agent_loop.lease import AgentLeaseController
from src.orchestration.agent_loop.models import (
    AgentCallOutcomeStatus,
    AgentFinishMetadata,
    AgentModelBinding,
    AgentModelContextLengthError,
    AgentRun,
    AgentRunStatus,
    AgentSample,
    AgentToolCall,
    AgentUsage,
    AgentUserMessageCommit,
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
from src.storage.sqlite.models import TaskNodeRow, TaskRow
from tests.orchestration.support import make_agent_result_projector


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


class _ContextAwareQueuedModel(_QueuedModel):
    def __init__(
        self,
        binding,
        outputs,
        *,
        context_error_on_main_calls: frozenset[int] = frozenset(),
    ) -> None:
        super().__init__(binding, outputs)
        self.context_error_on_main_calls = context_error_on_main_calls
        self.compaction_requests = []
        self.main_calls = 0

    async def sample_agent(self, request):
        if request.request_id.startswith("agent-compaction:"):
            self.requests.append(request)
            self.compaction_requests.append(request)
            return AgentSample(
                sample_id=f"summary-{len(self.compaction_requests)}",
                binding=self.binding,
                visible_text="compacted history facts",
                tool_calls=(),
                usage=AgentUsage(status="usage_unavailable"),
                finish=AgentFinishMetadata("stop", 1),
            )
        self.main_calls += 1
        if self.main_calls in self.context_error_on_main_calls:
            self.requests.append(request)
            raise AgentModelContextLengthError(
                "agent_model_context_length_exceeded"
            )
        return await super().sample_agent(request)


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


class _UnavailableSkillResultCandidateBuilder:
    async def build(self, **_kwargs):
        raise ValueError("agent_skill_result_artifact_unavailable")


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
        enable_preflight: bool = False,
        token_counter=None,
        candidate_builder_override=None,
    ):
        repository = repository or self.repository
        context_builder = AgentContextBuilder(
            AgentContextRules("stable", "tool rules", "final guard")
        )
        candidate_builder = candidate_builder_override or (
            AgentContextCandidateBuilder(
                context_builder=context_builder,
                token_counter=token_counter,
            )
            if enable_preflight
            else None
        )
        lease_controller = AgentLeaseController(repository, ttl_seconds=30)
        compaction_service = (
            AgentCompactionService(
                runs=repository,
                writer=repository,
                model=model,
                lease_controller=lease_controller,
                candidate_builder=candidate_builder,
            )
            if candidate_builder is not None
            else None
        )
        return AgentLoopRunner(
            runs=repository,
            writer=repository,
            model=model,
            context_builder=context_builder,
            catalog_builder=self.catalog_builder,
            visibility_context=self.visibility,
            lease_controller=lease_controller,
            invoker=invoker,
            owner_id="worker-1",
            reasoning_delta_sink=reasoning_delta_sink,
            reasoning_reset_sink=reasoning_reset_sink,
            terminal_event_recorder=terminal_event_recorder,
            context_candidate_builder=candidate_builder,
            compaction_service=compaction_service,
        )

    async def _initialize_budgeted_run(self, window: int) -> None:
        run = await self.repository.get_run("run-1")
        await self.repository.commit_agent_user_message(
            AgentUserMessageCommit(
                run_id=run.run_id,
                expected_revision=run.revision,
                expected_claim_token=None,
                text="current question",
                context_budget=AgentContextBudget.from_model_context_window(
                    window
                ),
            )
        )

    async def _run_two_wave_context_error_case(
        self,
        error_calls: frozenset[int],
    ):
        await self._initialize_budgeted_run(1_000_000)
        first_call = AgentToolCall(
            "first", self.names["skill.safe_one"], "{}", 0
        )
        second_call = AgentToolCall(
            "second", self.names["skill.safe_two"], "{}", 0
        )
        model = _ContextAwareQueuedModel(
            self.binding,
            [("", (first_call,)), ("", (second_call,)), ("final", ())],
            context_error_on_main_calls=error_calls,
        )
        invoker = _RecordingInvoker()
        result = await self._runner(
            model,
            invoker,
            enable_preflight=True,
            token_counter=lambda fragments, _binding: sum(
                len(fragment) for fragment in fragments
            ),
        ).run("run-1")
        return result, model, invoker

    async def test_total_preflight_fits_without_compaction_model_call(self) -> None:
        await self._initialize_budgeted_run(100_000)
        model = _ContextAwareQueuedModel(
            self.binding,
            [("final answer", ())],
        )

        result = await self._runner(
            model,
            _RecordingInvoker(),
            enable_preflight=True,
            token_counter=lambda fragments, _binding: sum(
                len(fragment) for fragment in fragments
            ),
        ).run("run-1")

        self.assertEqual(result.state, "final_candidate")
        self.assertEqual(len(model.requests), 1)
        self.assertEqual(model.compaction_requests, [])

    async def test_provider_context_error_compacts_once_without_skill_replay(
        self,
    ) -> None:
        result, model, invoker = await self._run_two_wave_context_error_case(
            frozenset({3})
        )

        self.assertEqual(result.state, "final_candidate")
        self.assertEqual(model.main_calls, 4)
        self.assertEqual(len(model.compaction_requests), 1)
        self.assertNotIn(
            "current question",
            model.compaction_requests[0].messages[1].content or "",
        )
        self.assertEqual(invoker.events.count("start:first"), 1)
        self.assertEqual(invoker.events.count("start:second"), 1)

    async def test_provider_context_error_without_history_fails_once(self) -> None:
        await self._initialize_budgeted_run(100_000)
        model = _ContextAwareQueuedModel(
            self.binding,
            [("unused", ())],
            context_error_on_main_calls=frozenset({1}),
        )
        invoker = _RecordingInvoker()

        result = await self._runner(
            model,
            invoker,
            enable_preflight=True,
            token_counter=lambda fragments, _binding: sum(
                len(fragment) for fragment in fragments
            ),
        ).run("run-1")

        self.assertEqual(result.state, "failed")
        self.assertEqual(model.main_calls, 1)
        self.assertEqual(model.compaction_requests, [])
        self.assertEqual(invoker.events, [])

    async def test_second_provider_context_error_fails_without_more_compaction(
        self,
    ) -> None:
        result, model, invoker = await self._run_two_wave_context_error_case(
            frozenset({3, 4})
        )

        self.assertEqual(result.state, "failed")
        self.assertEqual(
            result.run.terminal_reason_code,
            "agent_context_required_segments_too_large",
        )
        self.assertEqual(len(model.compaction_requests), 1)
        self.assertEqual(invoker.events.count("start:first"), 1)
        self.assertEqual(invoker.events.count("start:second"), 1)

    async def test_required_context_over_limit_fails_before_provider_or_skill(
        self,
    ) -> None:
        await self._initialize_budgeted_run(100)
        model = _ContextAwareQueuedModel(
            self.binding,
            [("must not run", ())],
        )
        invoker = _RecordingInvoker()

        result = await self._runner(
            model,
            invoker,
            enable_preflight=True,
            token_counter=lambda fragments, _binding: (
                1_000 if fragments else 0
            ),
        ).run("run-1")

        self.assertEqual(result.state, "failed")
        self.assertEqual(
            result.run.terminal_reason_code,
            "agent_context_required_segments_too_large",
        )
        self.assertEqual(model.requests, [])
        self.assertEqual(invoker.events, [])

    async def test_unavailable_skill_result_artifact_fails_before_model_or_skill(
        self,
    ) -> None:
        await self._initialize_budgeted_run(100_000)
        model = _ContextAwareQueuedModel(
            self.binding,
            [("must not run", ())],
        )
        invoker = _RecordingInvoker()

        result = await self._runner(
            model,
            invoker,
            candidate_builder_override=(
                _UnavailableSkillResultCandidateBuilder()
            ),
        ).run("run-1")

        self.assertEqual(result.state, "failed")
        self.assertEqual(
            result.run.terminal_reason_code,
            "agent_skill_result_artifact_unavailable",
        )
        self.assertEqual(model.requests, [])
        self.assertEqual(invoker.events, [])

    async def test_total_over_limit_compacts_closed_history_not_latest_result(
        self,
    ) -> None:
        await self._initialize_budgeted_run(35_000)
        first_call = AgentToolCall(
            "first", self.names["skill.safe_one"], "{}", 0
        )
        second_call = AgentToolCall(
            "second", self.names["skill.safe_two"], "{}", 0
        )
        model = _ContextAwareQueuedModel(
            self.binding,
            [("", (first_call,)), ("", (second_call,)), ("final", ())],
        )
        invoker = _RecordingInvoker(
            {
                "first": AgentCallExecution(
                    AgentCallOutcomeStatus.COMPLETED,
                    safe_result_payload={"blob": "a" * 18_000},
                ),
                "second": AgentCallExecution(
                    AgentCallOutcomeStatus.COMPLETED,
                    safe_result_payload={"blob": "b" * 18_000},
                ),
            }
        )

        result = await self._runner(
            model,
            invoker,
            enable_preflight=True,
            token_counter=lambda fragments, _binding: sum(
                len(fragment) for fragment in fragments
            ),
        ).run("run-1")

        self.assertEqual(result.state, "final_candidate")
        self.assertEqual(len(model.compaction_requests), 1)
        self.assertEqual(invoker.events.count("start:first"), 1)
        self.assertEqual(invoker.events.count("start:second"), 1)
        run = await self.repository.get_run("run-1")
        self.assertGreater(run.compacted_through_sequence, 1)
        items = await self.repository.list_items("run-1")
        second_result = next(
            item
            for item in items
            if item.kind.value == "tool_result"
            and "b" * 100 in item.payload_json
        )
        self.assertGreater(
            second_result.sequence, run.compacted_through_sequence
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

    async def test_same_batch_exact_calls_invoke_external_path_once(self) -> None:
        await self._initialize_budgeted_run(100_000)
        calls = (
            AgentToolCall(
                "duplicate-leader",
                self.names["skill.safe_one"],
                '{"query":"rice"}',
                0,
            ),
            AgentToolCall(
                "duplicate-follower",
                self.names["skill.safe_one"],
                '{"query":"rice"}',
                1,
            ),
        )
        model = _QueuedModel(
            self.binding,
            [("", calls), ("final answer", ())],
        )

        class Invocation:
            def __init__(inner_self) -> None:
                inner_self.calls = 0

            async def invoke(inner_self, _request, node, **_kwargs):
                inner_self.calls += 1
                execution = CapabilityExecutionResult(
                    capability_id="skill.safe_one",
                    task_id="task-1",
                    node_id=node.node_id,
                    output_payload={"answer": "ROOT-RESULT"}
                )
                return InvocationResult(
                    node=replace(node, status=NodeStatus.COMPLETED),
                    output_payload=dict(execution.output_payload),
                    execution_result=execution,
                )

        invocation = Invocation()

        async def load_task(_task_id):
            return Task(
                "task-1",
                "conv-1",
                "message-1",
                status=TaskStatus.RUNNING,
            )

        async def load_node(node_id):
            return TaskNode(
                node_id,
                "task-1",
                "skill.safe_one",
                status=NodeStatus.PENDING,
            )

        invoker = AgentCapabilityInvoker(
            invocation_service=invocation,
            runs=self.repository,
            task_loader=load_task,
            node_loader=load_node,
            request_metadata_loader=lambda _run: {},
            current_user_input_loader=lambda _run: "",
            result_projector=make_agent_result_projector(),
        )

        class ResponseLostRepository:
            def __init__(inner_self, repository) -> None:
                inner_self.repository = repository
                inner_self.commits = 0
                inner_self.lost = False

            def __getattr__(inner_self, name):
                return getattr(inner_self.repository, name)

            async def commit_agent_call_outcome(inner_self, commit):
                committed = await inner_self.repository.commit_agent_call_outcome(
                    commit
                )
                inner_self.commits += 1
                if inner_self.commits == 2:
                    inner_self.lost = True
                    raise RuntimeError("receipt response lost")
                return committed

        repository = ResponseLostRepository(self.repository)

        result = await self._runner(
            model,
            invoker,
            repository=repository,
        ).run("run-1")

        self.assertEqual(result.state, "final_candidate")
        self.assertTrue(repository.lost)
        items = await self.repository.list_items("run-1")
        self.assertEqual(
            invocation.calls,
            1,
            [
                (item.sequence, item.kind.value, item.payload_json)
                for item in items
            ],
        )
        results = [
            item
            for item in items
            if item.kind.value == "tool_result"
            and item.state.value == "committed"
        ]
        self.assertEqual(len(results), 2)
        root_payload = json.loads(results[0].payload_json)
        follower_payload = json.loads(results[1].payload_json)
        self.assertEqual(
            root_payload["safe_result"]["model_view"]["answer"],
            "ROOT-RESULT",
        )
        self.assertEqual(
            follower_payload["safe_result"]["source_result_item_id"],
            results[0].item_id,
        )
        by_id = {item.item_id: item for item in items}
        follower_call = by_id[results[1].source_call_item_id]
        follower_node_id = json.loads(follower_call.payload_json)["node_id"]
        with self.sessions() as session:
            follower_node = session.get(TaskNodeRow, follower_node_id)
        self.assertEqual(follower_node.status, "completed")
        self.assertIsNone(follower_node.assigned_instance_id)
        self.assertIsNone(follower_node.started_at)

    async def test_same_batch_waiting_leader_never_invokes_follower(self) -> None:
        calls = (
            AgentToolCall("leader", self.names["skill.safe_one"], "{}", 0),
            AgentToolCall("follower", self.names["skill.safe_one"], "{}", 1),
        )
        model = _QueuedModel(self.binding, [("", calls)])
        invoker = _RecordingInvoker(
            {
                "leader": AgentCallExecution(
                    AgentCallOutcomeStatus.WAITING_FOR_INPUT,
                    safe_result_payload={"leader": "waiting"},
                )
            }
        )

        result = await self._runner(model, invoker).run("run-1")

        self.assertEqual(result.state, "waiting")
        self.assertEqual(invoker.events, ["start:leader", "end:leader"])
        items = await self.repository.list_items("run-1")
        follower = next(
            json.loads(item.payload_json)
            for item in items
            if item.kind.value == "tool_result" and item.call_ordinal == 1
        )
        self.assertEqual(
            follower["safe_error_code"],
            "duplicate_call_leader_waiting",
        )

    async def test_same_batch_aborted_leader_never_invokes_follower(self) -> None:
        calls = (
            AgentToolCall("leader", self.names["skill.safe_one"], "{}", 0),
            AgentToolCall("follower", self.names["skill.safe_one"], "{}", 1),
        )
        model = _QueuedModel(
            self.binding,
            [("", calls), ("final answer", ())],
        )
        invoker = _RecordingInvoker(
            {
                "leader": AgentCallExecution(
                    AgentCallOutcomeStatus.ABORTED,
                    safe_error_code="leader_aborted",
                )
            }
        )

        result = await self._runner(model, invoker).run("run-1")

        self.assertEqual(result.state, "final_candidate")
        self.assertEqual(invoker.events, ["start:leader", "end:leader"])
        items = await self.repository.list_items("run-1")
        follower = next(
            json.loads(item.payload_json)
            for item in items
            if item.kind.value == "tool_result" and item.call_ordinal == 1
        )
        self.assertEqual(
            follower["safe_error_code"],
            "duplicate_call_leader_aborted",
        )

    async def test_same_batch_failed_leader_allows_sequential_follower(self) -> None:
        calls = (
            AgentToolCall("leader", self.names["skill.safe_one"], "{}", 0),
            AgentToolCall("follower", self.names["skill.safe_one"], "{}", 1),
        )
        model = _QueuedModel(
            self.binding,
            [("", calls), ("final answer", ())],
        )
        invoker = _RecordingInvoker(
            {
                "leader": AgentCallExecution(
                    AgentCallOutcomeStatus.FAILED,
                    safe_error_code="leader_failed",
                )
            }
        )

        result = await self._runner(model, invoker).run("run-1")

        self.assertEqual(result.state, "final_candidate")
        self.assertEqual(
            invoker.events,
            [
                "start:leader",
                "end:leader",
                "start:follower",
                "end:follower",
            ],
        )

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
