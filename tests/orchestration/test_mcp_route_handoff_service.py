from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from src.core.enums import NodeStatus, TaskStatus
from src.core.models import Task, TaskNode
from src.orchestration.backpressure import BackpressureGuard
from src.orchestration.completion_policy import CompletionPolicy
from src.orchestration.models import (
    CapabilityDescriptor,
    ExecutionInstance,
    InstanceState,
    OrchestrationRequest,
    UserMCPServerProfile,
    WorkflowNodePlan,
    WorkflowPlan,
)
from src.orchestration.registry import CapabilityRegistry, InstanceRegistry
from src.orchestration.scheduler import Scheduler
from src.orchestration.service import OrchestrationService
from tests.orchestration.support import (
    FakeExecutor,
    OrchestrationSQLiteTestCase,
    error_result,
    success_result,
)


class MCPRouteHandoffServiceTest(OrchestrationSQLiteTestCase):
    @staticmethod
    def _profile(server_id: str) -> UserMCPServerProfile:
        return UserMCPServerProfile(
            server_id,
            server_id,
            "route test",
            "streamable_http",
        )

    def _service(self, captured, *, execution_result=None):
        capability_registry = CapabilityRegistry()
        capability_registry.register(
            CapabilityDescriptor("mcp.dispatch", "dispatch", "dispatch")
        )
        capability_registry.register(
            CapabilityDescriptor("main.agent", "agent", "agent")
        )
        instance_registry = InstanceRegistry()
        instance_registry.register(
            ExecutionInstance(
                "instance-a",
                ("mcp.dispatch", "main.agent"),
                InstanceState.ONLINE,
                0,
            )
        )
        scheduler = Mock(wraps=Scheduler(instance_registry))

        def execute(request):
            captured.append(request)
            return execution_result or success_result(output_payload={"ok": True})

        service = OrchestrationService(
            storage=self.storage,
            capability_registry=capability_registry,
            instance_registry=instance_registry,
            scheduler=scheduler,
            executor=FakeExecutor(
                {
                    "mcp.dispatch": execute,
                    "main.agent": execute,
                }
            ),
            completion_policy=CompletionPolicy(),
            backpressure=BackpressureGuard(max_active_tasks=4),
        )
        return service, scheduler

    def _save_ready_node(self, *, task_id: str, node_id: str) -> TaskNode:
        asyncio.run(
            self.storage.save_task(
                Task(
                    task_id=task_id,
                    conversation_id="conversation-a",
                    root_message_id="message-a",
                    status=TaskStatus.RUNNING,
                )
            )
        )
        return asyncio.run(
            self.storage.save_task_node(
                TaskNode(
                    node_id=node_id,
                    task_id=task_id,
                    capability_id="mcp.dispatch",
                    status=NodeStatus.READY,
                )
            )
        )

    def test_auto_and_explicit_routes_reach_executor_with_equivalent_inputs(self) -> None:
        captured = []
        service, scheduler = self._service(captured)
        auto_node = self._save_ready_node(task_id="task-auto", node_id="node-auto")
        explicit_node = self._save_ready_node(
            task_id="task-explicit",
            node_id="node-explicit",
        )
        auto_request = OrchestrationRequest(
            "task-auto",
            "conversation-a",
            "message-a",
            "extract text",
            metadata={"mcp_binding_mode": "automatic"},
            available_mcp_servers=(self._profile("server-a"),),
        )
        explicit_request = OrchestrationRequest(
            "task-explicit",
            "conversation-a",
            "message-a",
            "extract text",
            metadata={
                "mcp_binding_mode": "explicit_command",
                "mcp_dispatch_server_id": "server-a",
                "forced_by_mcp_command": True,
                "mcp_command": "$OCR",
            },
        )
        auto_plan = WorkflowNodePlan(
            "node-auto",
            "mcp.dispatch",
            input_payload={"server_id": "server-a"},
            metadata={"mcp_binding_mode": "automatic"},
        )
        explicit_plan = WorkflowNodePlan(
            "node-explicit",
            "mcp.dispatch",
            input_payload={"server_id": "server-a"},
            metadata={
                "mcp_binding_mode": "explicit_command",
                "mcp_dispatch_server_id": "server-a",
                "forced_by_mcp_command": True,
                "mcp_command": "$OCR",
            },
        )

        asyncio.run(
            service._execute_node(
                auto_request,
                auto_plan,
                auto_node,
                dependency_outputs={},
            )
        )
        asyncio.run(
            service._execute_node(
                explicit_request,
                explicit_plan,
                explicit_node,
                dependency_outputs={},
            )
        )

        self.assertEqual(scheduler.select_instance.call_count, 2)
        self.assertEqual(captured[0].input_payload, captured[1].input_payload)
        self.assertEqual(captured[0].metadata, captured[1].metadata)
        self.assertEqual(
            captured[0].metadata,
            {"mcp_binding_mode": "explicit_command"},
        )

    def test_unauthorized_route_fails_task_before_scheduler_and_executor(self) -> None:
        captured = []
        service, scheduler = self._service(captured)
        request = OrchestrationRequest(
            "task-rejected",
            "conversation-a",
            "message-a",
            "extract text",
            available_mcp_servers=(self._profile("server-b"),),
        )
        plan = WorkflowPlan(
            task_id=request.task_id,
            nodes=(
                WorkflowNodePlan(
                    "node-rejected",
                    "mcp.dispatch",
                    input_payload={"server_id": "server-a"},
                ),
            ),
        )

        result = asyncio.run(service.execute_request(request, plan, active_task_count=0))
        node = asyncio.run(self.storage.get_task_node("node-rejected"))
        events = asyncio.run(self.storage.list_events_for_task(request.task_id))

        self.assertEqual(result.task.status, TaskStatus.FAILED)
        self.assertEqual(node.status, NodeStatus.FAILED)
        self.assertIsNotNone(node.finished_at)
        self.assertEqual(scheduler.select_instance.call_count, 0)
        self.assertEqual(captured, [])
        failed_event = next(event for event in events if event.event_type == "node.failed")
        self.assertEqual(failed_event.payload, {"code": "mcp_selected_route_not_authorized"})

    def test_pinned_route_conflict_fails_before_scheduler_and_executor(self) -> None:
        captured = []
        service, scheduler = self._service(captured)
        request = OrchestrationRequest(
            "task-pinned-conflict",
            "conversation-a",
            "message-a",
            "extract text",
            metadata={"mcp_dispatch_server_id": "server-b"},
            available_mcp_servers=(self._profile("server-a"),),
        )
        plan = WorkflowPlan(
            task_id=request.task_id,
            nodes=(
                WorkflowNodePlan(
                    "node-pinned-conflict",
                    "mcp.dispatch",
                    input_payload={"server_id": "server-a"},
                ),
            ),
        )

        result = asyncio.run(service.execute_request(request, plan, active_task_count=0))

        self.assertEqual(result.task.status, TaskStatus.FAILED)
        self.assertEqual(
            asyncio.run(
                self.storage.get_task_node("node-pinned-conflict")
            ).status,
            NodeStatus.FAILED,
        )
        self.assertEqual(scheduler.select_instance.call_count, 0)
        self.assertEqual(captured, [])

    def test_malformed_payload_keeps_existing_executor_error(self) -> None:
        captured = []
        service, scheduler = self._service(
            captured,
            execution_result=error_result(
                code="mcp_dispatch_payload_invalid",
                message="invalid payload",
            ),
        )
        request = OrchestrationRequest(
            "task-malformed",
            "conversation-a",
            "message-a",
            "extract text",
        )
        plan = WorkflowPlan(
            task_id=request.task_id,
            nodes=(
                WorkflowNodePlan(
                    "node-malformed",
                    "mcp.dispatch",
                    input_payload={"server_id": "server-a", "extra": True},
                ),
            ),
        )

        result = asyncio.run(service.execute_request(request, plan, active_task_count=0))
        events = asyncio.run(self.storage.list_events_for_task(request.task_id))

        self.assertEqual(result.task.status, TaskStatus.FAILED)
        self.assertEqual(scheduler.select_instance.call_count, 1)
        self.assertEqual(len(captured), 1)
        failed_event = next(event for event in events if event.event_type == "node.failed")
        self.assertEqual(failed_event.payload["code"], "mcp_dispatch_payload_invalid")

    def test_rejection_cas_loss_to_cancellation_returns_cancellation_authority(self) -> None:
        captured = []
        service, scheduler = self._service(captured)
        node = self._save_ready_node(task_id="task-cancel", node_id="node-cancel")
        request = OrchestrationRequest(
            "task-cancel",
            "conversation-a",
            "message-a",
            "extract text",
        )
        plan = WorkflowNodePlan(
            "node-cancel",
            "mcp.dispatch",
            input_payload={"server_id": "server-a"},
        )

        async def lose_to_cancellation(updated, *, expected_from_status):
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            task = await self.storage.get_task("task-cancel")
            await self.storage.save_task(
                replace(
                    task,
                    status=TaskStatus.CANCELLING,
                    cancel_requested_at=now,
                )
            )
            await self.storage.save_task_node(
                replace(node, status=NodeStatus.CANCELLED, finished_at=now)
            )
            return None

        with patch.object(
            self.storage,
            "compare_and_set_task_node",
            side_effect=lose_to_cancellation,
        ):
            updated, output = asyncio.run(
                service._execute_node(
                    request,
                    plan,
                    node,
                    dependency_outputs={},
                )
            )

        self.assertEqual(updated.status, NodeStatus.CANCELLED)
        self.assertEqual(output, {})
        self.assertEqual(scheduler.select_instance.call_count, 0)
        self.assertEqual(captured, [])

    def test_rejection_cas_loss_to_other_worker_raises_conflict(self) -> None:
        captured = []
        service, scheduler = self._service(captured)
        node = self._save_ready_node(task_id="task-conflict", node_id="node-conflict")
        request = OrchestrationRequest(
            "task-conflict",
            "conversation-a",
            "message-a",
            "extract text",
        )
        plan = WorkflowNodePlan(
            "node-conflict",
            "mcp.dispatch",
            input_payload={"server_id": "server-a"},
        )

        async def lose_to_worker(updated, *, expected_from_status):
            await self.storage.save_task_node(
                replace(node, status=NodeStatus.RUNNING)
            )
            return None

        with patch.object(
            self.storage,
            "compare_and_set_task_node",
            side_effect=lose_to_worker,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "^mcp_selected_route_rejection_conflict$",
            ):
                asyncio.run(
                    service._execute_node(
                        request,
                        plan,
                        node,
                        dependency_outputs={},
                    )
                )

        self.assertEqual(
            asyncio.run(self.storage.get_task_node("node-conflict")).status,
            NodeStatus.RUNNING,
        )
        self.assertEqual(scheduler.select_instance.call_count, 0)
        self.assertEqual(captured, [])

    def test_continuation_ownership_precedes_route_authority(self) -> None:
        captured = []
        service, scheduler = self._service(captured)
        node = self._save_ready_node(task_id="task-lease", node_id="node-lease")
        request = OrchestrationRequest(
            "task-lease",
            "conversation-a",
            "message-a",
            "extract text",
            metadata={"mcp_remote_task_continuation_id": "outbox-a"},
        )
        plan = WorkflowNodePlan(
            "node-lease",
            "mcp.dispatch",
            input_payload={"server_id": "server-a"},
        )

        with patch.object(
            self.storage,
            "compare_and_set_task_node",
            wraps=self.storage.compare_and_set_task_node,
        ) as compare_and_set:
            with self.assertRaisesRegex(
                RuntimeError,
                "^mcp_continuation_claim_token_missing$",
            ):
                asyncio.run(
                    service._execute_node(
                        request,
                        plan,
                        node,
                        dependency_outputs={},
                    )
                )

        compare_and_set.assert_not_awaited()
        self.assertEqual(scheduler.select_instance.call_count, 0)
        self.assertEqual(captured, [])
        self.assertEqual(
            asyncio.run(self.storage.get_task_node("node-lease")).status,
            NodeStatus.READY,
        )

    def test_two_mcp_nodes_keep_their_edge_and_are_normalized_independently(self) -> None:
        captured = []
        service, scheduler = self._service(captured)
        request = OrchestrationRequest(
            "task-two-servers",
            "conversation-a",
            "message-a",
            "use both servers",
            available_mcp_servers=(
                self._profile("server-a"),
                self._profile("server-b"),
            ),
        )
        plan = WorkflowPlan(
            task_id=request.task_id,
            nodes=(
                WorkflowNodePlan(
                    "node-a",
                    "mcp.dispatch",
                    input_payload={"server_id": "server-a"},
                ),
                WorkflowNodePlan(
                    "node-b",
                    "mcp.dispatch",
                    input_payload={"server_id": "server-b"},
                    depends_on=("node-a",),
                ),
            ),
        )

        result = asyncio.run(service.execute_request(request, plan, active_task_count=0))
        edges = asyncio.run(self.storage.list_task_edges(request.task_id))

        self.assertEqual(result.task.status, TaskStatus.COMPLETED)
        self.assertEqual(len(result.nodes), 2)
        self.assertEqual([(edge.from_node_id, edge.to_node_id) for edge in edges], [("node-a", "node-b")])
        self.assertEqual(
            [execution.input_payload["server_id"] for execution in captured],
            ["server-a", "server-b"],
        )
        self.assertTrue(
            all(
                execution.metadata["mcp_binding_mode"] == "explicit_command"
                for execution in captured
            )
        )
        self.assertEqual(scheduler.select_instance.call_count, 2)


if __name__ == "__main__":
    import unittest

    unittest.main()
