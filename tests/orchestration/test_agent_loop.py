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
from src.orchestration.agent_loop.runner import AgentCallExecution, AgentLoopRunner
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


class _RecordingInvoker:
    def __init__(self, outcomes=None, *, trace: list[str] | None = None) -> None:
        self.events = []
        self.outcomes = dict(outcomes or {})
        self.trace = trace

    async def invoke(self, *, call, effective_payload, **_kwargs):
        if self.trace is not None:
            self.trace.append("capability.invoke")
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

    def _runner(self, model, invoker, *, repository=None):
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
        )

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
        items = await self.repository.list_items("run-1")
        committed_results = [item for item in items if item.kind.value == "tool_result"]
        self.assertEqual([item.call_ordinal for item in committed_results], [0, 1, 2, 3])

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
