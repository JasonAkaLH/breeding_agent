from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from src.core.contracts import CapabilityExecutionResult
from src.core.enums import NodeStatus, TaskStatus
from src.core.models import Conversation, EventRecord, MCPRemoteTaskBinding, Task, TaskNode
from src.orchestration.backpressure import BackpressureGuard
from src.orchestration.completion_policy import CompletionPolicy
from src.orchestration.models import CapabilityDescriptor, ExecutionInstance, InstanceState, OrchestrationRequest, WorkflowNodePlan, WorkflowPlan
from src.orchestration.registry import CapabilityRegistry, InstanceRegistry
from src.orchestration.scheduler import Scheduler
from src.orchestration.service import OrchestrationService
from src.storage.sqlite.models import MCPRemoteTaskOutboxRow
from tests.orchestration.support import FakeExecutor, OrchestrationSQLiteTestCase, error_result, success_result


class FakeCapabilityFlowTest(OrchestrationSQLiteTestCase):
    def test_remote_task_continuation_preserves_completed_dispatch_node(self) -> None:
        capability_registry = CapabilityRegistry()
        capability_registry.register(CapabilityDescriptor("mcp.dispatch", "dispatch", "dispatch"))
        capability_registry.register(CapabilityDescriptor("cap.respond", "respond", "respond"))
        instance_registry = InstanceRegistry()
        instance_registry.register(
            ExecutionInstance("inst-1", ("mcp.dispatch", "cap.respond"), InstanceState.ONLINE, 0)
        )
        downstream_inputs = []

        def respond(request):
            downstream_inputs.append(request.dependency_outputs)
            return success_result(output_payload={"answer": "done"})

        service = OrchestrationService(
            storage=self.storage,
            capability_registry=capability_registry,
            instance_registry=instance_registry,
            scheduler=Scheduler(instance_registry),
            executor=FakeExecutor({"cap.respond": respond}),
            completion_policy=CompletionPolicy(),
            backpressure=BackpressureGuard(max_active_tasks=2),
        )
        asyncio.run(
            self.storage.save_task(
                Task("task-resume", "conv-1", "msg-1", status=TaskStatus.RUNNING)
            )
        )
        asyncio.run(
            self.storage.save_task_node(
                TaskNode(
                    "node-mcp",
                    "task-resume",
                    "mcp.dispatch",
                    status=NodeStatus.COMPLETED,
                    output_refs=("mcp-result-ref",),
                )
            )
        )
        continuation_token = "continuation-token-1"
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        with self.session_factory.begin() as session:
            session.add(
                MCPRemoteTaskOutboxRow(
                    outbox_id="continuation-1",
                    kind="terminal_continuation",
                    owner_user_id="alice",
                    task_id="task-resume",
                    node_id="node-mcp",
                    call_ref="call-1",
                    safe_remote_task_ref="remote-1",
                    payload={},
                    status="applied",
                    revision=1,
                    created_at=now,
                    updated_at=now,
                    continuation_admitted_at=now,
                    continuation_status="running",
                    continuation_claim_owner="runtime-1",
                    continuation_claim_token=continuation_token,
                    continuation_lease_expires_at=now + timedelta(minutes=1),
                    continuation_revision=2,
                )
            )
        request = OrchestrationRequest(
            "task-resume",
            "conv-1",
            "msg-1",
            "lookup",
            metadata={
                "mcp_remote_task_continuation_id": "continuation-1",
                "mcp_remote_task_source_node_id": "node-mcp",
                "mcp_remote_task_result": {"structuredContent": {"answer": 42}},
            },
        )
        plan = WorkflowPlan(
            task_id="task-resume",
            nodes=(
                WorkflowNodePlan("node-mcp", "mcp.dispatch"),
                WorkflowNodePlan("node-response", "cap.respond", depends_on=("node-mcp",)),
            ),
        )

        def assert_rejected(
            invalid_request: OrchestrationRequest, expected_code: str
        ) -> None:
            with self.assertRaisesRegex(RuntimeError, f"^{expected_code}$"):
                asyncio.run(
                    service.execute_request(invalid_request, plan, active_task_count=0)
                )
            self.assertEqual(downstream_inputs, [])
            self.assertIsNone(asyncio.run(self.storage.get_task_node("node-response")))
            self.assertEqual(asyncio.run(self.storage.list_task_edges("task-resume")), [])

        invalid_tokens = (("missing", None), ("non-string", 42), ("empty", ""))
        for case, invalid_token in invalid_tokens:
            invalid_request = request
            if case != "missing":
                invalid_request = replace(
                    request,
                    metadata={
                        **request.metadata,
                        "mcp_remote_task_continuation_claim_token": invalid_token,
                    },
                )
            with self.subTest(claim_token=case):
                assert_rejected(
                    invalid_request, "mcp_continuation_claim_token_missing"
                )

        request = replace(
            request,
            metadata={
                **request.metadata,
                "mcp_remote_task_continuation_claim_token": continuation_token,
            },
        )
        with self.subTest(claim_token="wrong"):
            assert_rejected(
                replace(
                    request,
                    metadata={
                        **request.metadata,
                        "mcp_remote_task_continuation_claim_token": "wrong-token",
                    },
                ),
                "mcp_continuation_execution_lease_lost",
            )

        with self.session_factory.begin() as session:
            row = session.get(MCPRemoteTaskOutboxRow, "continuation-1")
            row.continuation_lease_expires_at = now - timedelta(seconds=1)
        with self.subTest(lease="expired"):
            assert_rejected(request, "mcp_continuation_execution_lease_lost")
        with self.session_factory.begin() as session:
            row = session.get(MCPRemoteTaskOutboxRow, "continuation-1")
            row.continuation_lease_expires_at = now + timedelta(minutes=1)
            row.continuation_status = "abandoning"
        with self.subTest(status="abandoning"):
            assert_rejected(request, "mcp_continuation_execution_lease_lost")
        with self.session_factory.begin() as session:
            row = session.get(MCPRemoteTaskOutboxRow, "continuation-1")
            row.continuation_status = "running"

        result = asyncio.run(service.execute_request(request, plan, active_task_count=0))
        dispatch = asyncio.run(self.storage.get_task_node("node-mcp"))

        self.assertEqual(result.task.status, TaskStatus.COMPLETED)
        self.assertEqual(dispatch.status, NodeStatus.COMPLETED)
        self.assertEqual(dispatch.output_refs, ("mcp-result-ref",))
        self.assertEqual(
            downstream_inputs,
            [{"node-mcp": {"structuredContent": {"answer": 42}}}],
        )

    def test_remote_mcp_task_keeps_dispatch_node_nonterminal(self) -> None:
        capability_registry = CapabilityRegistry()
        capability_registry.register(
            CapabilityDescriptor(
                capability_id="mcp.dispatch",
                name="dispatch",
                description="dispatch",
            )
        )
        instance_registry = InstanceRegistry()
        instance_registry.register(
            ExecutionInstance(
                instance_id="inst-1",
                supported_capabilities=("mcp.dispatch",),
                state=InstanceState.ONLINE,
                load_score=0,
            )
        )
        service = OrchestrationService(
            storage=self.storage,
            capability_registry=capability_registry,
            instance_registry=instance_registry,
            scheduler=Scheduler(instance_registry),
            executor=FakeExecutor(
                {
                    "mcp.dispatch": success_result(
                        output_payload={
                            "mcp_status": "remote_task_created",
                            "safe_call_ref": "call-safe",
                            "safe_remote_task_ref": "remote-safe",
                        }
                    )
                }
            ),
            completion_policy=CompletionPolicy(),
            backpressure=BackpressureGuard(max_active_tasks=2),
        )
        request = OrchestrationRequest(
            task_id="task-mcp-remote",
            conversation_id="conv-1",
            root_message_id="msg-1",
            user_message="lookup",
        )
        plan = WorkflowPlan(
            task_id=request.task_id,
            nodes=(
                WorkflowNodePlan(
                    node_id="node-mcp",
                    capability_id="mcp.dispatch",
                ),
            ),
        )
        asyncio.run(self.storage.save_conversation(Conversation("conv-1", "alice")))
        asyncio.run(
            self.storage.save_mcp_remote_task_binding(
                MCPRemoteTaskBinding(
                    safe_remote_task_ref="remote-safe",
                    owner_user_id="alice",
                    task_id=request.task_id,
                    node_id="node-mcp",
                    call_ref="call-safe",
                    server_id="server-safe",
                    protocol_version="2026-07-28",
                    remote_task_ciphertext=b"sealed",
                    remote_task_nonce=b"nonce",
                    encryption_version=1,
                    last_status="working",
                )
            )
        )

        result = asyncio.run(service.execute_request(request, plan, active_task_count=0))
        node = asyncio.run(self.storage.get_task_node("node-mcp"))

        self.assertEqual(result.task.status, TaskStatus.RUNNING)
        self.assertEqual(node.status, NodeStatus.WAITING_FOR_DEPENDENCY)
        self.assertIsNone(node.finished_at)

    def test_fake_capability_flow_runs_to_completion(self) -> None:
        capability_registry = CapabilityRegistry()
        capability_registry.register(CapabilityDescriptor(capability_id="cap.route", name="route", description="route"))
        capability_registry.register(CapabilityDescriptor(capability_id="cap.respond", name="respond", description="respond"))

        instance_registry = InstanceRegistry()
        instance_registry.register(ExecutionInstance(instance_id="inst-1", supported_capabilities=("cap.route", "cap.respond"), state=InstanceState.ONLINE, load_score=0))

        executor = FakeExecutor(
            {
                "cap.route": success_result(output_payload={"route": "default"}),
                "cap.respond": success_result(output_payload={"answer": "done"}),
            }
        )

        service = OrchestrationService(
            storage=self.storage,
            capability_registry=capability_registry,
            instance_registry=instance_registry,
            scheduler=Scheduler(instance_registry),
            executor=executor,
            completion_policy=CompletionPolicy(),
            backpressure=BackpressureGuard(max_active_tasks=2),
        )

        request = OrchestrationRequest(task_id="task-1", conversation_id="conv-1", root_message_id="msg-1", user_message="hello")
        plan = WorkflowPlan(
            task_id="task-1",
            nodes=(
                WorkflowNodePlan(node_id="node-1", capability_id="cap.route"),
                WorkflowNodePlan(node_id="node-2", capability_id="cap.respond", depends_on=("node-1",)),
            ),
        )

        result = asyncio.run(service.execute_request(request, plan, active_task_count=0))
        stored_task = asyncio.run(self.storage.get_task("task-1"))
        stored_nodes = asyncio.run(self.storage.list_task_nodes_for_task("task-1"))
        stored_events = asyncio.run(self.storage.list_events_for_task("task-1"))

        self.assertEqual(result.task.status, TaskStatus.COMPLETED)
        self.assertEqual(stored_task.status, TaskStatus.COMPLETED)
        self.assertEqual([node.status for node in stored_nodes], ["completed", "completed"])
        self.assertGreaterEqual(len(stored_events), 4)

    def test_executor_events_without_timestamp_are_persisted_with_created_at(self) -> None:
        capability_registry = CapabilityRegistry()
        capability_registry.register(CapabilityDescriptor(capability_id="cap.respond", name="respond", description="respond"))

        instance_registry = InstanceRegistry()
        instance_registry.register(ExecutionInstance(instance_id="inst-1", supported_capabilities=("cap.respond",), state=InstanceState.ONLINE, load_score=0))

        def handler(request):
            return CapabilityExecutionResult(
                capability_id=request.capability_id,
                task_id=request.task_id,
                node_id=request.node_id,
                output_payload={"answer": "done"},
                events=(EventRecord(
                    event_id="custom-progress",
                    conversation_id=request.conversation_id,
                    task_id=request.task_id,
                    node_id=request.node_id,
                    event_type="custom.progress",
                    payload={"step": "working"},
                ),),
            )

        service = OrchestrationService(
            storage=self.storage,
            capability_registry=capability_registry,
            instance_registry=instance_registry,
            scheduler=Scheduler(instance_registry),
            executor=FakeExecutor({"cap.respond": handler}),
            completion_policy=CompletionPolicy(),
            backpressure=BackpressureGuard(max_active_tasks=2),
        )

        request = OrchestrationRequest(task_id="task-event-time", conversation_id="conv-1", root_message_id="msg-1", user_message="hello")
        plan = WorkflowPlan(task_id="task-event-time", nodes=(WorkflowNodePlan(node_id="node-1", capability_id="cap.respond"),))

        asyncio.run(service.execute_request(request, plan, active_task_count=0))
        stored_events = asyncio.run(self.storage.list_events_for_task("task-event-time"))

        custom = next(event for event in stored_events if event.event_id == "custom-progress")
        self.assertIsNotNone(custom.created_at)

    def test_node_started_frontend_event_exposes_skill_name_from_plan_metadata(self) -> None:
        capability_registry = CapabilityRegistry()
        capability_registry.register(CapabilityDescriptor(capability_id="skill.demo_query", name="demo", description="demo"))

        instance_registry = InstanceRegistry()
        instance_registry.register(ExecutionInstance(instance_id="inst-1", supported_capabilities=("skill.demo_query",), state=InstanceState.ONLINE, load_score=0))

        service = OrchestrationService(
            storage=self.storage,
            capability_registry=capability_registry,
            instance_registry=instance_registry,
            scheduler=Scheduler(instance_registry),
            executor=FakeExecutor({"skill.demo_query": success_result(output_payload={"answer": "done"})}),
            completion_policy=CompletionPolicy(),
            backpressure=BackpressureGuard(max_active_tasks=2),
        )

        request = OrchestrationRequest(task_id="task-skill-name", conversation_id="conv-1", root_message_id="msg-1", user_message="hello")
        plan = WorkflowPlan(
            task_id="task-skill-name",
            nodes=(WorkflowNodePlan(node_id="node-1", capability_id="skill.demo_query", metadata={"skill_name": "demo-query"}),),
        )

        asyncio.run(service.execute_request(request, plan, active_task_count=0))
        stored_events = asyncio.run(self.storage.list_events_for_task("task-skill-name"))

        started = next(event for event in stored_events if event.event_type == "node.started")
        completed = next(event for event in stored_events if event.event_type == "node.completed")
        self.assertEqual(started.payload["capability_id"], "skill.demo_query")
        self.assertEqual(started.payload["skill_name"], "demo-query")
        self.assertEqual(completed.payload["capability_id"], "skill.demo_query")
        self.assertEqual(completed.payload["skill_name"], "demo-query")

    def test_required_failure_does_not_complete_task(self) -> None:
        capability_registry = CapabilityRegistry()
        capability_registry.register(CapabilityDescriptor(capability_id="cap.route", name="route", description="route"))

        instance_registry = InstanceRegistry()
        instance_registry.register(ExecutionInstance(instance_id="inst-1", supported_capabilities=("cap.route",), state=InstanceState.ONLINE, load_score=0))

        executor = FakeExecutor({"cap.route": error_result(code="boom", message="failed")})

        service = OrchestrationService(
            storage=self.storage,
            capability_registry=capability_registry,
            instance_registry=instance_registry,
            scheduler=Scheduler(instance_registry),
            executor=executor,
            completion_policy=CompletionPolicy(),
            backpressure=BackpressureGuard(max_active_tasks=2),
        )

        request = OrchestrationRequest(task_id="task-2", conversation_id="conv-1", root_message_id="msg-2", user_message="hello")
        plan = WorkflowPlan(task_id="task-2", nodes=(WorkflowNodePlan(node_id="node-1", capability_id="cap.route"),), max_replans=0)

        result = asyncio.run(service.execute_request(request, plan, active_task_count=0))
        self.assertEqual(result.task.status, TaskStatus.FAILED)
