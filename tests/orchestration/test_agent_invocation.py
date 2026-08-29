from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from src.core.contracts import (
    CapabilityExecutionError,
    CapabilityExecutionRequest,
    CapabilityExecutionResult,
)
from src.core.enums import NodeStatus, TaskStatus
from src.core.models import Artifact, Task, TaskNode
from src.orchestration.agent_loop.invocation import (
    CapabilityInvocationService,
    InvocationRequest,
)
from src.orchestration.agent_loop.lease import AgentLeaseHandle
from src.orchestration.agent_loop.capability_invoker import AgentCapabilityInvoker
from src.orchestration.agent_loop.continuation import (
    AgentContinuationLocator,
    AgentResumeKind,
)
from src.orchestration.agent_loop.context_budget import AgentContextBudget
from src.orchestration.agent_loop.models import (
    AgentCallOutcomeCommit,
    AgentCallOutcomeStatus,
    AgentItem,
    AgentItemKind,
    AgentItemState,
    AgentModelBinding,
    AgentRun,
    AgentRunStatus,
    AgentToolCall,
    AgentTaskLease,
)
from src.orchestration.agent_loop.task_projection import (
    AgentTaskInvocationCommitPort,
)
from src.orchestration.agent_loop.transient_results import (
    AgentTransientSkillResultStage,
)
from src.orchestration.models import ExecutionInstance, InstanceState
from src.orchestration.registry import InstanceRegistry
from src.orchestration.instance_selector import InstanceSelector
from tests.orchestration.support import FakeExecutor


class _RecordingCommitPort:
    def __init__(self, *, task: Task, node: TaskNode) -> None:
        self.task = task
        self.node = node
        self.steps: list[str] = []

    async def assert_execution_owned(self, request: InvocationRequest) -> None:
        self.steps.append("owned")

    async def start_node(
        self,
        request: InvocationRequest,
        node: TaskNode,
        *,
        instance_id: str,
        started_at: datetime,
        activity_payload: dict[str, Any],
    ) -> TaskNode:
        self.steps.append("start")
        self.node = replace(
            node,
            status=NodeStatus.RUNNING,
            assigned_instance_id=instance_id,
            started_at=started_at,
        )
        return self.node

    async def get_task_snapshot(self, task_id: str) -> Task | None:
        return self.task

    async def get_node_snapshot(self, node_id: str) -> TaskNode | None:
        return self.node

    async def commit_completed(
        self,
        request: InvocationRequest,
        node: TaskNode,
        result: CapabilityExecutionResult,
        *,
        now: datetime,
        activity_payload: dict[str, Any],
    ) -> TaskNode:
        self.steps.append("completed")
        self.node = replace(
            node,
            status=NodeStatus.COMPLETED,
            finished_at=now,
            output_refs=tuple(artifact.artifact_id for artifact in result.artifacts),
        )
        return self.node

    async def commit_failed(self, *args: Any, **kwargs: Any) -> TaskNode:
        self.steps.append("failed")
        return self.node

    async def commit_waiting_for_input(self, *args: Any, **kwargs: Any) -> TaskNode:
        self.steps.append("waiting_for_input")
        return self.node

    async def commit_waiting_for_dependency(self, *args: Any, **kwargs: Any) -> TaskNode:
        self.steps.append("waiting_for_dependency")
        return self.node

    async def discard_late_result(self, *args: Any, **kwargs: Any) -> TaskNode:
        self.steps.append("late_result")
        return self.node

    async def commit_route_rejected(self, *args: Any, **kwargs: Any) -> TaskNode:
        self.steps.append("route_rejected")
        return self.node


class _RecordingAgentAtomicWriter:
    def __init__(self) -> None:
        self.outcomes: list[AgentCallOutcomeCommit] = []

    async def commit_agent_call_outcome(self, commit: AgentCallOutcomeCommit) -> None:
        self.outcomes.append(commit)


class _AgentFixtureCommitPort(_RecordingCommitPort):
    def __init__(
        self,
        *,
        task: Task,
        node: TaskNode,
        writer: _RecordingAgentAtomicWriter,
    ) -> None:
        super().__init__(task=task, node=node)
        self.writer = writer

    async def commit_completed(
        self,
        request: InvocationRequest,
        node: TaskNode,
        result: CapabilityExecutionResult,
        *,
        now: datetime,
        activity_payload: dict[str, Any],
    ) -> TaskNode:
        if (
            request.run_id is None
            or request.call_item_id is None
            or request.expected_revision is None
        ):
            raise AssertionError("Agent fixture identity missing")
        await self.writer.commit_agent_call_outcome(
            AgentCallOutcomeCommit(
                run_id=request.run_id,
                expected_revision=request.expected_revision,
                expected_claim_token=request.expected_claim_token,
                call_item_id=request.call_item_id,
                safe_result_payload=dict(result.output_payload),
                status=AgentCallOutcomeStatus.COMPLETED,
            )
        )
        return await super().commit_completed(
            request,
            node,
            result,
            now=now,
            activity_payload=activity_payload,
        )


class CapabilityInvocationServiceTest(unittest.IsolatedAsyncioTestCase):
    async def test_budgeted_large_skill_stages_private_result_without_artifact(
        self,
    ) -> None:
        capability_id = "skill.large"
        instances = InstanceRegistry()
        instances.register(
            ExecutionInstance(
                "instance-large", (capability_id,), InstanceState.ONLINE, 0
            )
        )
        task = Task("task-1", "conv-1", "message-1", status=TaskStatus.RUNNING)
        node = TaskNode(
            "node-1", task.task_id, capability_id, status=NodeStatus.PENDING
        )
        port = _RecordingCommitPort(task=task, node=node)
        kernel = CapabilityInvocationService(
            instance_selector=InstanceSelector(instances),
            executor=FakeExecutor(
                {
                    capability_id: CapabilityExecutionResult(
                        capability_id=capability_id,
                        task_id=task.task_id,
                        node_id=node.node_id,
                        output_payload={"records": ["x" * 150_000]},
                    )
                }
            ),
            commit_port=port,
            now_fn=lambda: datetime(2026, 8, 28, 12, 0),
        )
        run = AgentRun(
            run_id="run-1",
            task_id=task.task_id,
            conversation_id=task.conversation_id,
            status=AgentRunStatus.RUNNING,
            binding=AgentModelBinding("edition-a"),
            revision=3,
        )
        budget = AgentContextBudget.from_model_context_window(450_000)
        user_payload = json.dumps(
            {"context_budget": budget.to_payload(), "text": "question"},
            sort_keys=True,
            separators=(",", ":"),
        )
        user_item = AgentItem(
            "user-1",
            run.run_id,
            run.task_id,
            1,
            AgentItemKind.USER_MESSAGE,
            AgentItemState.COMMITTED,
            user_payload,
            "a" * 64,
        )
        call_item = AgentItem(
            "call-item-1",
            run.run_id,
            run.task_id,
            2,
            AgentItemKind.TOOL_CALL,
            AgentItemState.COMMITTED,
            '{"node_id":"node-1"}\n',
            "b" * 64,
        )
        reservation = AgentItem(
            "result-item-1",
            run.run_id,
            run.task_id,
            3,
            AgentItemKind.TOOL_RESULT,
            AgentItemState.RESERVED,
            "{}\n",
            "c" * 64,
            source_call_item_id=call_item.item_id,
        )

        class Runs:
            async def list_items(_self, _run_id: str):
                return (user_item, call_item, reservation)

        transient_calls: list[dict[str, object]] = []

        def stage_transient(**values: object) -> AgentTransientSkillResultStage:
            transient_calls.append(values)
            raw = values["canonical_raw_bytes"]
            assert isinstance(raw, bytes)
            return AgentTransientSkillResultStage(
                stage_ref=str(values["expected_stage_ref"]),
                raw_size_bytes=len(raw),
                raw_sha256=str(values["raw_sha256"]),
                projection_revision=str(values["projection_revision"]),
            )

        invoker = AgentCapabilityInvoker(
            invocation_service=kernel,
            runs=Runs(),
            task_loader=AsyncMock(return_value=task),
            node_loader=AsyncMock(return_value=node),
            request_metadata_loader=lambda _run: {},
            current_user_input_loader=lambda _run: "question",
            legacy_result_artifact_stager=lambda **_values: self.fail(
                "v2 transient result must not call legacy Artifact stager"
            ),
            transient_result_stager=stage_transient,
        )

        outcome = await invoker.invoke(
            run=run,
            call=AgentToolCall("call-1", "skill_large", "{}", 0),
            call_item=call_item,
            result_reservation=reservation,
            capability_id=capability_id,
            effective_payload={},
            cancellation=None,
        )

        self.assertEqual(outcome.status, AgentCallOutcomeStatus.COMPLETED)
        self.assertEqual(outcome.staged_artifacts, ())
        self.assertEqual(
            outcome.safe_result_payload["projection_mode"],
            "transient_staged",
        )
        self.assertEqual(len(transient_calls), 1)
        self.assertEqual(
            transient_calls[0]["result_item_id"], reservation.item_id
        )

        def fail_stage(**_values: object) -> None:
            raise RuntimeError("injected")

        invoker._stage_transient_result = fail_stage  # noqa: SLF001
        failed = await invoker.invoke(
            run=run,
            call=AgentToolCall("call-1", "skill_large", "{}", 0),
            call_item=call_item,
            result_reservation=reservation,
            capability_id=capability_id,
            effective_payload={},
            cancellation=None,
        )
        self.assertEqual(failed.status, AgentCallOutcomeStatus.FAILED)
        self.assertEqual(
            failed.safe_error_code,
            "agent_transient_skill_result_stage_failed",
        )

    async def test_agent_terminal_projection_is_candidate_only_until_outcome_cas(self) -> None:
        storage = SimpleNamespace(
            save_task_node=AsyncMock(),
            compare_and_set_task_node=AsyncMock(),
        )
        record_event = AsyncMock()
        port = AgentTaskInvocationCommitPort(
            storage=storage,
            runs=SimpleNamespace(),
            make_event=lambda **values: values,
            record_event=record_event,
        )
        request = InvocationRequest(
            capability_id="skill.lookup",
            conversation_id="conv-1",
            task_id="task-1",
            node_id="node-1",
            run_id="run-1",
            call_item_id="call-1",
        )
        node = TaskNode(
            "node-1", "task-1", "skill.lookup", status=NodeStatus.RUNNING
        )
        result = CapabilityExecutionResult(
            "skill.lookup", "task-1", "node-1", output_payload={"answer": 1}
        )
        now = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)

        completed = await port.commit_completed(
            request,
            node,
            result,
            now=now,
            activity_payload={"capability_id": "skill.lookup"},
        )
        failed = await port.commit_failed(
            request,
            node,
            replace(
                result,
                error=CapabilityExecutionError("failed", "failed"),
            ),
            now=now,
            activity_payload={"capability_id": "skill.lookup"},
        )
        rejected = await port.commit_route_rejected(
            request,
            node,
            rejection_code="route_rejected",
            now=now,
            activity_payload={"capability_id": "skill.lookup"},
        )

        self.assertEqual(completed.status, NodeStatus.COMPLETED)
        self.assertEqual(failed.status, NodeStatus.FAILED)
        self.assertEqual(rejected.status, NodeStatus.FAILED)
        storage.save_task_node.assert_not_awaited()
        storage.compare_and_set_task_node.assert_not_awaited()
        record_event.assert_not_awaited()

    async def test_agent_invocation_refreshes_lease_after_long_executor_call(self) -> None:
        instances = InstanceRegistry()
        instances.register(
            ExecutionInstance("instance-1", ("cap.lookup",), InstanceState.ONLINE, 0)
        )
        task = Task("task-1", "conv-1", "message-1", status=TaskStatus.RUNNING)
        node = TaskNode("node-1", "task-1", "cap.lookup", status=NodeStatus.PENDING)
        expires = datetime.now(timezone.utc) + timedelta(seconds=30)
        handle = AgentLeaseHandle(
            AgentTaskLease("run-1", "task-1", "worker-1", "claim-1", 3, expires)
        )

        class LeaseCheckingPort(_RecordingCommitPort):
            def __init__(self) -> None:
                super().__init__(task=task, node=node)
                self.tokens: list[tuple[int | None, str | None]] = []

            async def assert_execution_owned(self, request: InvocationRequest) -> None:
                self.tokens.append(
                    (request.expected_revision, request.expected_claim_token)
                )
                if (
                    request.expected_revision != handle.current.revision
                    or request.expected_claim_token != handle.current.token
                ):
                    raise AssertionError("stale Agent lease snapshot")
                await super().assert_execution_owned(request)

        port = LeaseCheckingPort()

        def execute(_request: CapabilityExecutionRequest):
            handle.current = AgentTaskLease(
                "run-1", "task-1", "worker-1", "claim-2", 4, expires
            )
            return CapabilityExecutionResult(
                "cap.lookup", "task-1", "node-1", output_payload={"answer": 42}
            )

        kernel = CapabilityInvocationService(
            instance_selector=InstanceSelector(instances),
            executor=FakeExecutor({"cap.lookup": execute}),
            commit_port=port,
        )

        async def load_task(_task_id: str) -> Task:
            return task

        async def load_node(_node_id: str) -> TaskNode:
            return node

        run = AgentRun(
            run_id="run-1",
            task_id="task-1",
            conversation_id="conv-1",
            status=AgentRunStatus.RUNNING,
            binding=AgentModelBinding("edition-a"),
            claim_token="claim-1",
            revision=3,
        )

        class Runs:
            async def get_run(self, _run_id: str) -> AgentRun:
                return replace(
                    run,
                    claim_token=handle.current.token,
                    revision=handle.current.revision,
                )

        call_item = AgentItem(
            item_id="call-item-1",
            run_id=run.run_id,
            task_id=run.task_id,
            sequence=1,
            kind=AgentItemKind.TOOL_CALL,
            state=AgentItemState.COMMITTED,
            payload_json='{"node_id":"node-1"}',
            payload_sha256="0" * 64,
        )
        invoker = AgentCapabilityInvoker(
            invocation_service=kernel,
            runs=Runs(),
            task_loader=load_task,
            node_loader=load_node,
            request_metadata_loader=lambda _run: {},
            current_user_input_loader=lambda _run: "",
        )

        outcome = await invoker.invoke(
            run=run,
            call=AgentToolCall("call-1", "cap_lookup", "{}", 0),
            call_item=call_item,
            result_reservation=object(),
            capability_id="cap.lookup",
            effective_payload={},
            cancellation=None,
            lease_handle=handle,
        )

        self.assertEqual(outcome.status, AgentCallOutcomeStatus.COMPLETED)
        self.assertEqual(port.tokens, [(3, "claim-1"), (4, "claim-2")])

    async def test_resume_forwards_recovery_lease_handle_to_invoke(self) -> None:
        run = AgentRun(
            "run-1",
            "task-1",
            "conv-1",
            AgentRunStatus.WAITING_FOR_INPUT,
            AgentModelBinding("edition-a"),
            active_sample_item_id="sample-1",
            waiting_call_item_ids=("call-item-1",),
            claim_token="claim-1",
            revision=3,
        )
        call_item = AgentItem(
            "call-item-1",
            run.run_id,
            run.task_id,
            2,
            AgentItemKind.TOOL_CALL,
            AgentItemState.COMMITTED,
            json.dumps(
                {
                    "arguments_json": '{"server_id":"server-1"}',
                    "call_id": "provider-call-1",
                    "capability_id": "mcp.dispatch",
                    "node_id": "node-1",
                    "provider_safe_name": "mcp_dispatch",
                },
                sort_keys=True,
            ),
            "a" * 64,
            parent_item_id="sample-1",
        )
        reservation = AgentItem(
            "result-item-1",
            run.run_id,
            run.task_id,
            3,
            AgentItemKind.TOOL_RESULT,
            AgentItemState.RESERVED,
            "{}",
            "b" * 64,
            source_call_item_id=call_item.item_id,
        )

        class Runs:
            async def get_run(self, _run_id):
                return run

            async def list_items(self, _run_id):
                return (call_item, reservation)

        invoker = AgentCapabilityInvoker(
            invocation_service=AsyncMock(),
            runs=Runs(),
            task_loader=AsyncMock(),
            node_loader=AsyncMock(),
            request_metadata_loader=lambda _run: {"mcp_dispatch_server_id": "server-1"},
            current_user_input_loader=lambda _run: "",
        )
        invoker.invoke = AsyncMock(
            return_value=SimpleNamespace(status=AgentCallOutcomeStatus.COMPLETED)
        )
        handle = AgentLeaseHandle(
            AgentTaskLease(
                "run-1",
                "task-1",
                "worker-1",
                "claim-1",
                3,
                datetime.now(timezone.utc) + timedelta(seconds=30),
            )
        )
        locator = AgentContinuationLocator(
            run_id=run.run_id,
            sample_item_id="sample-1",
            call_item_id=call_item.item_id,
            provider_call_id="provider-call-1",
            capability_id="mcp.dispatch",
            task_id=run.task_id,
            node_id="node-1",
            owner_scope="owner-1",
            conversation_id=run.conversation_id,
            resume_kind=AgentResumeKind.MCP_APPROVAL,
            authority_digest="c" * 64,
            pinned_bundle_revision=None,
            model_binding=run.binding,
        )

        await invoker.resume(locator, lease_handle=handle)

        self.assertIs(invoker.invoke.await_args.kwargs["lease_handle"], handle)

    async def test_waiting_branches_keep_execution_revalidation_and_semantic_commit_order(
        self,
    ) -> None:
        cases = (
            (
                "cap.lookup",
                CapabilityExecutionResult(
                    "cap.lookup",
                    "task-1",
                    "node-1",
                    output_payload={"question": "safe question"},
                    error=CapabilityExecutionError(
                        "skill_input_missing",
                        "input required",
                    ),
                ),
                "waiting_for_input",
            ),
            (
                "mcp.dispatch",
                CapabilityExecutionResult(
                    "mcp.dispatch",
                    "task-1",
                    "node-1",
                    output_payload={"mcp_status": "remote_task_created"},
                ),
                "waiting_for_dependency",
            ),
        )
        for capability_id, execution_result, waiting_step in cases:
            with self.subTest(capability_id=capability_id):
                instances = InstanceRegistry()
                instances.register(
                    ExecutionInstance(
                        "instance-1",
                        (capability_id,),
                        InstanceState.ONLINE,
                        0,
                    )
                )
                task = Task(
                    "task-1",
                    "conv-1",
                    "message-1",
                    status=TaskStatus.RUNNING,
                )
                node = TaskNode(
                    "node-1",
                    "task-1",
                    capability_id,
                    status=NodeStatus.PENDING,
                )
                port = _RecordingCommitPort(task=task, node=node)

                def execute(_request):
                    port.steps.append("execute")
                    return execution_result

                kernel = CapabilityInvocationService(
                    instance_selector=InstanceSelector(instances),
                    executor=FakeExecutor({capability_id: execute}),
                    commit_port=port,
                    now_fn=lambda: datetime(2026, 8, 22, 12, 0),
                )
                result = await kernel.invoke(
                    InvocationRequest(
                        capability_id,
                        "conv-1",
                        "task-1",
                        "node-1",
                    ),
                    node,
                )

                self.assertEqual(
                    port.steps,
                    ["owned", "start", "execute", "owned", waiting_step],
                )
                self.assertEqual(result.output_payload, execution_result.output_payload)

    async def test_agent_mcp_hook_wraps_the_real_dispatch_invocation(self) -> None:
        instances = InstanceRegistry()
        instances.register(
            ExecutionInstance(
                "instance-mcp",
                ("mcp.dispatch",),
                InstanceState.ONLINE,
                0,
            )
        )
        task = Task("task-1", "conv-1", "message-1", status=TaskStatus.RUNNING)
        node = TaskNode("node-1", "task-1", "mcp.dispatch", status=NodeStatus.PENDING)
        port = _RecordingCommitPort(task=task, node=node)
        timeline: list[str] = []

        def execute(request: CapabilityExecutionRequest) -> CapabilityExecutionResult:
            timeline.append("execute")
            self.assertEqual(request.input_payload, {"server_id": "server-1"})
            return CapabilityExecutionResult(
                capability_id=request.capability_id,
                task_id=request.task_id,
                node_id=request.node_id,
                output_payload={"status": "completed"},
            )

        kernel = CapabilityInvocationService(
            instance_selector=InstanceSelector(instances),
            executor=FakeExecutor({"mcp.dispatch": execute}),
            commit_port=port,
            now_fn=lambda: datetime(2026, 8, 23, 12, 0),
        )

        async def hook(**values: Any) -> object | None:
            phase = str(values["phase"])
            timeline.append(f"hook:{phase}")
            if phase == "begin":
                self.assertEqual(values["capability_id"], "mcp.dispatch")
                self.assertEqual(values["effective_payload"], {"server_id": "server-1"})
                return object()
            self.assertEqual(values["result"].node.status, NodeStatus.COMPLETED)
            return None

        async def load_task(_task_id: str) -> Task:
            return task

        async def load_node(_node_id: str) -> TaskNode:
            return node

        run = AgentRun(
            run_id="run-1",
            task_id="task-1",
            conversation_id="conv-1",
            status=AgentRunStatus.RUNNING,
            binding=AgentModelBinding("edition-a"),
            claim_token="claim-1",
            revision=3,
        )
        call_item = AgentItem(
            item_id="call-item-1",
            run_id=run.run_id,
            task_id=run.task_id,
            sequence=1,
            kind=AgentItemKind.TOOL_CALL,
            state=AgentItemState.COMMITTED,
            payload_json='{"node_id":"node-1"}',
            payload_sha256="0" * 64,
        )
        invoker = AgentCapabilityInvoker(
            invocation_service=kernel,
            runs=object(),
            task_loader=load_task,
            node_loader=load_node,
            request_metadata_loader=lambda _run: {},
            current_user_input_loader=lambda _run: "",
            invocation_hook=hook,
        )

        outcome = await invoker.invoke(
            run=run,
            call=AgentToolCall("call-1", "mcp_dispatch", "{}", 0),
            call_item=call_item,
            result_reservation=object(),
            capability_id="mcp.dispatch",
            effective_payload={"server_id": "server-1"},
            cancellation=None,
        )

        self.assertEqual(outcome.status, AgentCallOutcomeStatus.COMPLETED)
        self.assertEqual(timeline, ["hook:begin", "execute", "hook:finish"])

    async def test_agent_fixture_injects_only_agent_atomic_writer_for_outcome(self) -> None:
        instances = InstanceRegistry()
        instances.register(
            ExecutionInstance("instance-1", ("cap.lookup",), InstanceState.ONLINE, 0)
        )
        task = Task("task-1", "conv-1", "message-1", status=TaskStatus.RUNNING)
        node = TaskNode("node-1", "task-1", "cap.lookup", status=NodeStatus.PENDING)
        writer = _RecordingAgentAtomicWriter()
        port = _AgentFixtureCommitPort(task=task, node=node, writer=writer)
        kernel = CapabilityInvocationService(
            instance_selector=InstanceSelector(instances),
            executor=FakeExecutor(
                {
                    "cap.lookup": CapabilityExecutionResult(
                        capability_id="cap.lookup",
                        task_id="task-1",
                        node_id="node-1",
                        output_payload={"answer": 42},
                    )
                }
            ),
            commit_port=port,
            now_fn=lambda: datetime(2026, 8, 22, 12, 0),
        )

        await kernel.invoke(
            InvocationRequest(
                capability_id="cap.lookup",
                conversation_id="conv-1",
                task_id="task-1",
                node_id="node-1",
                run_id="run-1",
                call_item_id="call-item-1",
                expected_revision=3,
                expected_claim_token="claim-1",
                model_binding=AgentModelBinding("edition-a"),
            ),
            node,
        )

        self.assertEqual(len(writer.outcomes), 1)
        self.assertEqual(writer.outcomes[0].run_id, "run-1")
        self.assertEqual(writer.outcomes[0].call_item_id, "call-item-1")
        self.assertEqual(writer.outcomes[0].safe_result_payload, {"answer": 42})

    async def test_kernel_owns_single_executor_lifecycle_and_semantic_commit(self) -> None:
        instances = InstanceRegistry()
        instances.register(
            ExecutionInstance(
                "instance-1",
                ("cap.lookup",),
                InstanceState.ONLINE,
                0,
            )
        )
        task = Task("task-1", "conv-1", "message-1", status=TaskStatus.RUNNING)
        node = TaskNode("node-1", "task-1", "cap.lookup", status=NodeStatus.PENDING)
        port = _RecordingCommitPort(task=task, node=node)
        executor_requests: list[CapabilityExecutionRequest] = []

        def execute(request: CapabilityExecutionRequest) -> CapabilityExecutionResult:
            port.steps.append("execute")
            executor_requests.append(request)
            return CapabilityExecutionResult(
                capability_id=request.capability_id,
                task_id=request.task_id,
                node_id=request.node_id,
                output_payload={"answer": 42},
                artifacts=(
                    Artifact(
                        artifact_id="artifact-1",
                        task_id=request.task_id,
                        producer_node_id=request.node_id,
                        artifact_type="json",
                        storage_ref="opaque://artifact-1",
                    ),
                ),
            )

        kernel = CapabilityInvocationService(
            instance_selector=InstanceSelector(instances),
            executor=FakeExecutor({"cap.lookup": execute}),
            commit_port=port,
            now_fn=lambda: datetime(2026, 8, 22, 12, 0),
        )
        result = await kernel.invoke(
            InvocationRequest(
                capability_id="cap.lookup",
                conversation_id="conv-1",
                task_id="task-1",
                node_id="node-1",
                input_payload={"query": "safe"},
                dependency_outputs={"node-0": {"value": 1}},
                request_metadata={"request": "value"},
                node_metadata={"node": "value"},
            ),
            node,
        )

        self.assertEqual(port.steps, ["owned", "start", "execute", "owned", "completed"])
        self.assertEqual(result.node.status, NodeStatus.COMPLETED)
        self.assertEqual(result.output_payload, {"answer": 42})
        self.assertEqual(len(executor_requests), 1)
        self.assertEqual(executor_requests[0].metadata, {"node": "value", "request": "value"})

    async def test_late_result_is_classified_before_any_success_commit(self) -> None:
        instances = InstanceRegistry()
        instances.register(
            ExecutionInstance("instance-1", ("cap.lookup",), InstanceState.ONLINE, 0)
        )
        task = Task("task-1", "conv-1", "message-1", status=TaskStatus.RUNNING)
        node = TaskNode("node-1", "task-1", "cap.lookup", status=NodeStatus.PENDING)
        port = _RecordingCommitPort(task=task, node=node)

        def cancel_during_execute(request: CapabilityExecutionRequest) -> CapabilityExecutionResult:
            port.steps.append("execute")
            port.task = replace(task, status=TaskStatus.CANCELLED)
            port.node = replace(port.node, status=NodeStatus.CANCELLED)
            return CapabilityExecutionResult(
                capability_id=request.capability_id,
                task_id=request.task_id,
                node_id=request.node_id,
                output_payload={"must_not_commit": True},
            )

        kernel = CapabilityInvocationService(
            instance_selector=InstanceSelector(instances),
            executor=FakeExecutor({"cap.lookup": cancel_during_execute}),
            commit_port=port,
            now_fn=lambda: datetime(2026, 8, 22, 12, 0),
        )
        result = await kernel.invoke(
            InvocationRequest("cap.lookup", "conv-1", "task-1", "node-1"),
            node,
        )

        self.assertEqual(port.steps, ["owned", "start", "execute", "owned", "late_result"])
        self.assertEqual(result.node.status, NodeStatus.CANCELLED)
        self.assertEqual(result.output_payload, {})
