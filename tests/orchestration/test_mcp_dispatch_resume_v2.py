from __future__ import annotations

import asyncio

from src.core.enums import ArtifactType, EdgeType, NodeStatus, TaskStatus
from src.core.models import Artifact, Task, TaskEdge, TaskNode
from src.integrations.mcp.cp7_artifacts import canonical_sha256
from src.integrations.mcp.resume_envelope import (
    MCPDispatchResumeEnvelopeError,
    build_mcp_dispatch_resume_envelope_v2,
)
from src.orchestration.backpressure import BackpressureGuard
from src.orchestration.completion_policy import CompletionPolicy
from src.orchestration.models import (
    CapabilityDescriptor,
    ExecutionInstance,
    InstanceState,
    OrchestrationRequest,
)
from src.orchestration.registry import CapabilityRegistry, InstanceRegistry
from src.orchestration.scheduler import Scheduler
from src.orchestration.service import OrchestrationService
from tests.orchestration.support import (
    FakeExecutor,
    OrchestrationSQLiteTestCase,
    success_result,
)


class MCPDispatchResumeV2Test(OrchestrationSQLiteTestCase):
    def _service(self, captured):
        capability_registry = CapabilityRegistry()
        capability_registry.register(
            CapabilityDescriptor("mcp.dispatch", "dispatch", "dispatch")
        )
        instance_registry = InstanceRegistry()
        instance_registry.register(
            ExecutionInstance(
                "instance-a",
                ("mcp.dispatch",),
                InstanceState.ONLINE,
                0,
            )
        )

        def execute(request):
            captured.append(request)
            return success_result(output_payload={"safe_summary": "done"})

        return OrchestrationService(
            storage=self.storage,
            capability_registry=capability_registry,
            instance_registry=instance_registry,
            scheduler=Scheduler(instance_registry),
            executor=FakeExecutor({"mcp.dispatch": execute}),
            completion_policy=CompletionPolicy(),
            backpressure=BackpressureGuard(max_active_tasks=2),
        )

    def _fixtures(self, *, summary="durable dependency summary"):
        task = Task(
            task_id="task-v2-resume",
            conversation_id="conversation-a",
            root_message_id="message-a",
            status=TaskStatus.RUNNING,
            mcp_execution_mode="user_scoped",
            mcp_shadow_enabled=False,
            mcp_rollout_config_version="cp7",
            mcp_route_reason_code="enforce_selected",
            mcp_rollout_mode="enforce",
        )
        dependency = TaskNode(
            node_id="node-dependency",
            task_id=task.task_id,
            capability_id="main.agent",
            status=NodeStatus.COMPLETED,
            output_refs=("artifact-a",),
        )
        node = TaskNode(
            node_id="node-mcp",
            task_id=task.task_id,
            capability_id="mcp.dispatch",
            status=NodeStatus.READY,
        )
        edge = TaskEdge(dependency.node_id, node.node_id, EdgeType.DATA)
        artifact = Artifact(
            artifact_id="artifact-a",
            task_id=task.task_id,
            producer_node_id=dependency.node_id,
            artifact_type=ArtifactType.TEXT,
            storage_ref="artifact://a",
            summary=summary,
            is_complete=True,
        )
        asyncio.run(self.storage.save_task(task))
        asyncio.run(self.storage.save_task_node(dependency))
        asyncio.run(self.storage.save_task_node(node))
        asyncio.run(self.storage.save_task_edge(task.task_id, edge))
        asyncio.run(self.storage.save_artifact(artifact))
        envelope = build_mcp_dispatch_resume_envelope_v2(
            task=task,
            node=node,
            edges=(edge,),
            attachments=(),
            dependency_nodes=(dependency,),
            server_id="server-a",
        )
        request = OrchestrationRequest(
            task_id=task.task_id,
            conversation_id=task.conversation_id,
            root_message_id=task.root_message_id,
            user_message="root text",
            requested_capability_id="mcp.dispatch",
            metadata={
                "mcp_binding_mode": "automatic",
                "mcp_dispatch_server_id": "server-a",
                "user_message": "root text",
            },
        )
        return task, node, envelope, request

    def test_v2_rebuilds_input_and_dependency_projection_from_refs(self) -> None:
        _task, _node, envelope, request = self._fixtures()
        captured = []

        resumed, output = asyncio.run(
            self._service(captured).resume_persisted_mcp_dispatch_node(
                request,
                envelope,
                expected_envelope_sha256=canonical_sha256(envelope),
            )
        )

        self.assertEqual(resumed.status, NodeStatus.COMPLETED)
        self.assertEqual(output, {"safe_summary": "done"})
        self.assertEqual(captured[0].input_payload, {"server_id": "server-a"})
        self.assertEqual(
            captured[0].dependency_outputs,
            {
                "node-dependency": {
                    "safe_summary": "durable dependency summary",
                    "artifact_refs": ["artifact-a"],
                }
            },
        )
        self.assertEqual(captured[0].metadata["user_message"], "root text")
        self.assertNotIn("dependency_outputs", envelope)
        self.assertNotIn("metadata", envelope)

    def test_v2_missing_artifact_summary_fails_before_execution(self) -> None:
        _task, _node, envelope, request = self._fixtures(summary=None)
        captured = []

        with self.assertRaisesRegex(
            MCPDispatchResumeEnvelopeError,
            "mcp_dispatch_resume_dependency_unrecoverable",
        ):
            asyncio.run(
                self._service(captured).resume_persisted_mcp_dispatch_node(
                    request,
                    envelope,
                    expected_envelope_sha256=canonical_sha256(envelope),
                )
            )

        self.assertEqual(captured, [])
        self.assertEqual(
            asyncio.run(self.storage.get_task_node("node-mcp")).status,
            NodeStatus.READY,
        )
