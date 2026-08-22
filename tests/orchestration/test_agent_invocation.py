from __future__ import annotations

import inspect
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from src.core.contracts import CapabilityExecutionRequest, CapabilityExecutionResult
from src.core.enums import NodeStatus, TaskStatus
from src.core.models import Artifact, Task, TaskNode
from src.orchestration.agent_loop.invocation import (
    CapabilityInvocationService,
    InvocationRequest,
)
from src.orchestration.agent_loop.models import (
    AgentCallOutcomeCommit,
    AgentCallOutcomeStatus,
    AgentModelBinding,
)
from src.orchestration.models import ExecutionInstance, InstanceState
from src.orchestration.registry import InstanceRegistry
from src.orchestration.scheduler import Scheduler
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
            scheduler=Scheduler(instances),
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
            scheduler=Scheduler(instances),
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
            scheduler=Scheduler(instances),
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

    def test_orchestration_service_delegates_executor_lifecycle_to_kernel(self) -> None:
        from src.orchestration import service

        service_source = inspect.getsource(service.OrchestrationService._execute_node)
        self.assertNotIn("_executor.execute", service_source)
        self.assertIn("_invocation_service.invoke", service_source)
        invocation_source = (
            Path(__file__).resolve().parents[2]
            / "src"
            / "orchestration"
            / "agent_loop"
            / "invocation.py"
        ).read_text(encoding="utf-8")
        self.assertEqual(invocation_source.count("self._executor.execute("), 1)
