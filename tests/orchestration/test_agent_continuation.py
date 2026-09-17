from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from src.orchestration.agent_loop.context import AgentContextBuilder, AgentContextRules
from src.orchestration.agent_loop.continuation import (
    AgentContinuationLocatorService,
    AgentResumeKind,
)
from src.orchestration.agent_loop.lease import AgentLeaseController
from src.orchestration.agent_loop.models import (
    AgentCallOutcomeCommit,
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


class _QueuedModel:
    def __init__(self, binding, outputs) -> None:
        self.binding = binding
        self.outputs = iter(outputs)
        self.requests = []

    async def sample_agent(self, request):
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
            ),
        )


class _WaitingInvoker:
    def __init__(self, locators: AgentContinuationLocatorService) -> None:
        self._locators = locators
        self.events = []
        self.built_locators = {}

    async def invoke(self, *, run, call, call_item, **_kwargs):
        self.events.append(call.call_id)
        await asyncio.sleep(0)
        if call.call_id in {"skill-wait", "approval-wait"}:
            kind = (
                AgentResumeKind.SKILL_INPUT
                if call.call_id == "skill-wait"
                else AgentResumeKind.MCP_APPROVAL
            )
            locator = self._locators.build(
                run=run,
                call_item=call_item,
                owner_scope="owner-1",
                resume_kind=kind,
                authority_digest=hashlib.sha256(call.call_id.encode()).hexdigest(),
                pinned_bundle_revision="bundle-r7" if kind is AgentResumeKind.SKILL_INPUT else None,
            )
            self.built_locators[call.call_id] = locator
            return AgentCallExecution(
                kind.waiting_status,
                safe_result_payload={"continuation_locator": locator.to_safe_dict()},
            )
        return AgentCallExecution(
            AgentCallOutcomeStatus.COMPLETED,
            safe_result_payload={"status": "closed"},
        )


def _policy(*, parallel_safe: bool) -> CapabilityInvocationPolicy:
    return CapabilityInvocationPolicy(
        model_allowed_fields=(),
        input_schema={"type": "object", "additionalProperties": True},
        parallel_safe=parallel_safe,
    )


class AgentContinuationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.engine = create_sqlite_engine(Path(self.temp_dir.name) / "continuation.sqlite")
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
        self.binding = AgentModelBinding(
            "edition-a",
            reasoning_effort="high",
            option_digests={"system": "d" * 64},
        )
        await self.repository.create_run(
            AgentRun("run-1", "task-1", "conv-1", AgentRunStatus.RUNNING, self.binding)
        )
        registry = CapabilityRegistry()
        for capability_id, parallel_safe in (
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
                invocation_policy=_policy(parallel_safe=parallel_safe),
            )
        self.catalog_builder = AgentToolCatalogBuilder(registry)
        self.visibility = CapabilityVisibilityContext("owner-1")
        catalog = self.catalog_builder.build(self.visibility)
        self.names = {tool.capability_id: tool.provider_safe_name for tool in catalog.tools}
        self.locators = AgentContinuationLocatorService()

    async def asyncTearDown(self) -> None:
        self.engine.dispose()
        self.temp_dir.cleanup()
        await super().asyncTearDown()

    def _runner(self, model, invoker, *, owner_id="worker-1") -> AgentLoopRunner:
        return AgentLoopRunner(
            runs=self.repository,
            writer=self.repository,
            model=model,
            context_builder=AgentContextBuilder(
                AgentContextRules("stable", "tool rules", "final guard")
            ),
            catalog_builder=self.catalog_builder,
            visibility_context=self.visibility,
            lease_controller=AgentLeaseController(self.repository, ttl_seconds=30),
            invoker=invoker,
            owner_id=owner_id,
        )

    async def _complete_waiting(self, call_item_id: str, *, owner_id: str):
        leases = AgentLeaseController(self.repository, ttl_seconds=30)
        handle = await leases.acquire("run-1", owner_id=owner_id)
        run = await self.repository.get_run("run-1")
        assert run is not None
        await self.repository.commit_agent_call_outcome(
            AgentCallOutcomeCommit(
                run_id=run.run_id,
                expected_revision=run.revision,
                expected_claim_token=handle.current.token,
                call_item_id=call_item_id,
                safe_result_payload={"status": "continued"},
                status=AgentCallOutcomeStatus.COMPLETED,
            )
        )
        updated = await self.repository.get_run("run-1")
        assert updated is not None
        return leases, handle, updated

    async def test_two_waiting_calls_close_independently_then_resume_remaining_wave(self) -> None:
        calls = (
            AgentToolCall(
                "skill-wait",
                self.names["skill.safe_one"],
                '{"secret":"must-not-enter-locator"}',
                0,
            ),
            AgentToolCall("approval-wait", self.names["skill.safe_two"], "{}", 1),
            AgentToolCall("exclusive", self.names["skill.exclusive"], "{}", 2),
        )
        model = _QueuedModel(self.binding, [("", calls), ("final answer", ())])
        invoker = _WaitingInvoker(self.locators)
        runner = self._runner(model, invoker)

        waiting = await runner.run("run-1")

        self.assertEqual(waiting.state, "waiting")
        self.assertEqual(len(waiting.run.waiting_call_item_ids), 2)
        self.assertEqual(invoker.events, ["skill-wait", "approval-wait"])
        self.assertEqual(len(model.requests), 1)
        self.assertIsNone(waiting.run.claim_token)

        locator_values = tuple(invoker.built_locators.values())
        waiting_results = [
            json.loads(item.payload_json)
            for item in await self.repository.list_items("run-1")
            if item.kind.value == "tool_result" and item.state.value == "reserved"
        ]
        self.assertEqual(
            {
                payload["safe_result"]["continuation_locator"]["call_item_id"]
                for payload in waiting_results
                if "safe_result" in payload
            },
            set(waiting.run.waiting_call_item_ids),
        )
        safe_json = json.dumps([locator.to_safe_dict() for locator in locator_values])
        for forbidden in ("must-not-enter-locator", "arguments_json", "raw_result", "credential", "attachment"):
            self.assertNotIn(forbidden, safe_json)
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            self.locators.resolve_unique(
                locator_values,
                owner_scope="owner-1",
                conversation_id="conv-1",
                task_id="task-1",
            )
        skill_locator = self.locators.resolve_unique(
            locator_values,
            owner_scope="owner-1",
            conversation_id="conv-1",
            task_id="task-1",
            call_item_id=invoker.built_locators["skill-wait"].call_item_id,
        )
        self.assertEqual(skill_locator.model_binding, self.binding)
        self.assertEqual(len(skill_locator.digest), 64)
        self.assertEqual(
            self.locators.from_safe_dict(skill_locator.to_safe_dict()),
            skill_locator,
        )
        unsafe_locator = skill_locator.to_safe_dict()
        unsafe_locator["arguments_json"] = '{"secret":true}'
        with self.assertRaisesRegex(ValueError, "shape_invalid"):
            self.locators.from_safe_dict(unsafe_locator)

        leases, handle, one_left = await self._complete_waiting(
            skill_locator.call_item_id,
            owner_id="resume-1",
        )
        self.assertEqual(
            self.locators.remaining_waiting_calls(
                waiting.run,
                completed_call_item_id=skill_locator.call_item_id,
            ),
            one_left.waiting_call_item_ids,
        )
        await leases.release_waiting("run-1", handle=handle)

        still_waiting = await runner.run("run-1")
        self.assertEqual(still_waiting.state, "waiting")
        self.assertEqual(len(model.requests), 1)
        self.assertEqual(invoker.events, ["skill-wait", "approval-wait"])
        self.assertIsNone(still_waiting.run.claim_token)

        approval_locator = invoker.built_locators["approval-wait"]
        _, final_handle, resumed = await self._complete_waiting(
            approval_locator.call_item_id,
            owner_id="resume-2",
        )
        self.assertEqual(resumed.status, AgentRunStatus.RUNNING)
        self.assertEqual(resumed.waiting_call_item_ids, ())

        completed = await self._runner(
            model,
            invoker,
            owner_id="resume-2",
        ).run_claimed("run-1", handle=final_handle)

        self.assertEqual(completed.state, "final_candidate")
        self.assertEqual(invoker.events[-1], "exclusive")
        self.assertEqual(len(model.requests), 2)
        items = await self.repository.list_items("run-1")
        results = [item for item in items if item.kind.value == "tool_result" and item.state.value == "committed"]
        self.assertEqual([item.call_ordinal for item in results], [0, 1, 2])

    async def test_locator_rejects_owner_mismatch_and_maps_all_resume_kinds(self) -> None:
        self.assertEqual(
            AgentResumeKind.MCP_REMOTE_TASK.waiting_status,
            AgentCallOutcomeStatus.WAITING_FOR_DEPENDENCY,
        )
        for kind in (
            AgentResumeKind.SKILL_INPUT,
            AgentResumeKind.MCP_APPROVAL,
            AgentResumeKind.MCP_ELICITATION,
        ):
            self.assertEqual(kind.waiting_status, AgentCallOutcomeStatus.WAITING_FOR_INPUT)
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            self.locators.resolve_unique(
                (),
                owner_scope="wrong-owner",
                conversation_id="conv-1",
                task_id="task-1",
            )
