from __future__ import annotations

import asyncio
import unittest
from dataclasses import replace

from src.core.contracts import CapabilityExecutionRequest, CapabilityExecutionResult
from src.core.enums import NodeCriticality, NodeStatus, TaskStatus
from src.core.models import Task, TaskNode
from src.orchestration.backpressure import BackpressureGuard
from src.orchestration.completion_policy import CompletionPolicy, CompletionStatus
from src.orchestration.models import CapabilityDescriptor, ExecutionInstance, InstanceState, OrchestrationRequest, WorkflowNodePlan, WorkflowPlan
from src.orchestration.registry import CapabilityRegistry, InstanceRegistry
from src.orchestration.runtime_replanner import CompositeRuntimeReplanner, RuntimeReplanContext, RuntimeReplanDecision, RuntimeReplanner
from src.orchestration.scheduler import Scheduler
from src.orchestration.service import OrchestrationService
from tests.orchestration.support import FakeExecutor, OrchestrationSQLiteTestCase, error_result, success_result


class _FailureRepairReplanner(RuntimeReplanner):
    def build_replan(self, context: RuntimeReplanContext) -> RuntimeReplanDecision | None:
        if context.completion_status != CompletionStatus.REPLAN_AVAILABLE:
            return None
        return RuntimeReplanDecision(
            plan=WorkflowPlan(
                task_id=context.plan.task_id,
                nodes=(WorkflowNodePlan(node_id="repair", capability_id="cap.repair"),),
                metadata={"route": "repair"},
                max_replans=context.plan.max_replans,
                max_dynamic_nodes=context.plan.max_dynamic_nodes,
            ),
            reason="replace_failed_required_node",
        )


class _UnsatisfiedCompletionReplanner(RuntimeReplanner):
    def build_replan(self, context: RuntimeReplanContext) -> RuntimeReplanDecision | None:
        if context.completion_status != CompletionStatus.COMPLETED:
            return None
        if context.node_outputs.get("probe_more", {}).get("satisfied") is True:
            return None
        probe_output = context.node_outputs.get("probe", {})
        if probe_output.get("satisfied") is not False:
            return None
        return RuntimeReplanDecision(
            plan=WorkflowPlan(
                task_id=context.plan.task_id,
                nodes=(
                    context.plan.node_by_id("probe"),
                    WorkflowNodePlan(node_id="probe_more", capability_id="cap.probe_more", depends_on=("probe",)),
                ),
                metadata={"route": "expand_after_unsatisfied_result"},
                max_replans=context.plan.max_replans,
                max_dynamic_nodes=context.plan.max_dynamic_nodes,
            ),
            reason="completed_result_did_not_satisfy_user_need",
        )


class _TooManyNodesReplanner(RuntimeReplanner):
    def build_replan(self, context: RuntimeReplanContext) -> RuntimeReplanDecision | None:
        if context.completion_status != CompletionStatus.REPLAN_AVAILABLE:
            return None
        return RuntimeReplanDecision(
            plan=WorkflowPlan(
                task_id=context.plan.task_id,
                nodes=(
                    WorkflowNodePlan(node_id="n1", capability_id="cap.repair"),
                    WorkflowNodePlan(node_id="n2", capability_id="cap.repair"),
                ),
                max_replans=context.plan.max_replans,
                max_dynamic_nodes=context.plan.max_dynamic_nodes,
            ),
            reason="over_budget",
        )


class _UnsafePendingMutationReplanner(RuntimeReplanner):
    def build_replan(self, context: RuntimeReplanContext) -> RuntimeReplanDecision | None:
        if context.completion_status != CompletionStatus.RUNNING:
            return None
        if "search" not in context.node_outputs:
            return None
        return RuntimeReplanDecision(
            plan=WorkflowPlan(
                task_id=context.plan.task_id,
                nodes=(
                    context.plan.node_by_id("search"),
                    WorkflowNodePlan(node_id="refine", capability_id="cap.repair", depends_on=("search",)),
                    WorkflowNodePlan(node_id="answer", capability_id="cap.repair", depends_on=("refine",)),
                ),
                max_replans=context.plan.max_replans,
                max_dynamic_nodes=context.plan.max_dynamic_nodes,
            ),
            reason="unsafe_mutate_pending_answer_dependency",
        )


class _UnsafeCompletedMutationReplanner(RuntimeReplanner):
    def build_replan(self, context: RuntimeReplanContext) -> RuntimeReplanDecision | None:
        if context.completion_status != CompletionStatus.COMPLETED:
            return None
        if context.replan_count > 0:
            return None
        return RuntimeReplanDecision(
            plan=WorkflowPlan(
                task_id=context.plan.task_id,
                nodes=(WorkflowNodePlan(node_id="probe", capability_id="cap.repair"),),
                max_replans=context.plan.max_replans,
                max_dynamic_nodes=context.plan.max_dynamic_nodes,
            ),
            reason="unsafe_mutate_completed_node_capability",
        )


class _AsyncNoneReplanner(RuntimeReplanner):
    async def build_replan(self, context: RuntimeReplanContext) -> RuntimeReplanDecision | None:
        return None


class _SyncDecisionReplanner(RuntimeReplanner):
    def build_replan(self, context: RuntimeReplanContext) -> RuntimeReplanDecision | None:
        if context.completion_status != CompletionStatus.REPLAN_AVAILABLE:
            return None
        return RuntimeReplanDecision(
            plan=WorkflowPlan(task_id=context.plan.task_id, nodes=(WorkflowNodePlan(node_id="repair", capability_id="cap.repair"),), max_replans=1, max_dynamic_nodes=1),
            reason="sync_after_async_none",
        )


class RuntimeReplanningTest(OrchestrationSQLiteTestCase):
    def _service(self, *, executor: FakeExecutor, runtime_replanner: RuntimeReplanner) -> OrchestrationService:
        capability_registry = CapabilityRegistry()
        for capability_id in ("cap.fail", "cap.repair", "cap.probe", "cap.probe_more"):
            capability_registry.register(CapabilityDescriptor(capability_id=capability_id, name=capability_id, description=capability_id))
        instance_registry = InstanceRegistry()
        instance_registry.register(
            ExecutionInstance(
                instance_id="inst-1",
                supported_capabilities=("cap.fail", "cap.repair", "cap.probe", "cap.probe_more"),
                state=InstanceState.ONLINE,
            )
        )
        return OrchestrationService(
            storage=self.storage,
            capability_registry=capability_registry,
            instance_registry=instance_registry,
            scheduler=Scheduler(instance_registry),
            executor=executor,
            completion_policy=CompletionPolicy(),
            backpressure=BackpressureGuard(max_active_tasks=2),
            runtime_replanner=runtime_replanner,
        )

    def test_required_failure_can_be_replanned_to_replacement_nodes(self) -> None:
        replanned_snapshots: list[dict] = []

        def repair_handler(request: CapabilityExecutionRequest) -> CapabilityExecutionResult:
            replanned_snapshots.append(
                dict(request.metadata["mcp_remote_task_continuation_plan"])
            )
            return replace(
                success_result(output_payload={"ok": True}),
                capability_id=request.capability_id,
                task_id=request.task_id,
                node_id=request.node_id,
            )

        service = self._service(
            executor=FakeExecutor({"cap.fail": error_result(code="boom", message="failed"), "cap.repair": repair_handler}),
            runtime_replanner=_FailureRepairReplanner(),
        )
        request = OrchestrationRequest(task_id="task-repair", conversation_id="conv-1", root_message_id="msg-1", user_message="repair it")
        plan = WorkflowPlan(
            task_id="task-repair",
            nodes=(WorkflowNodePlan(node_id="fail", capability_id="cap.fail"),),
            max_replans=1,
            max_dynamic_nodes=1,
        )

        result = asyncio.run(service.execute_request(request, plan, active_task_count=0))
        nodes = {node.node_id: node for node in asyncio.run(self.storage.list_task_nodes_for_task("task-repair"))}
        events = asyncio.run(self.storage.list_events_for_task("task-repair"))

        self.assertEqual(result.task.status, TaskStatus.COMPLETED)
        self.assertEqual(nodes["fail"].status, NodeStatus.FAILED)
        self.assertEqual(nodes["repair"].status, NodeStatus.COMPLETED)
        self.assertTrue(any(event.event_type == "task.replanned" for event in events))
        self.assertTrue(any(event.event_type == "task.graph_updated" for event in events))
        self.assertEqual(
            [node["node_id"] for node in replanned_snapshots[0]["nodes"]],
            ["repair"],
        )

    def test_completed_but_unsatisfied_result_can_append_more_nodes_before_task_completion(self) -> None:
        def probe_handler(request: CapabilityExecutionRequest) -> CapabilityExecutionResult:
            return replace(success_result(output_payload={"satisfied": False}), capability_id=request.capability_id, task_id=request.task_id, node_id=request.node_id)

        service = self._service(
            executor=FakeExecutor({
                "cap.probe": probe_handler,
                "cap.probe_more": success_result(output_payload={"satisfied": True}),
            }),
            runtime_replanner=_UnsatisfiedCompletionReplanner(),
        )
        request = OrchestrationRequest(task_id="task-expand", conversation_id="conv-1", root_message_id="msg-1", user_message="answer fully")
        plan = WorkflowPlan(
            task_id="task-expand",
            nodes=(WorkflowNodePlan(node_id="probe", capability_id="cap.probe"),),
            max_replans=1,
            max_dynamic_nodes=1,
        )

        result = asyncio.run(service.execute_request(request, plan, active_task_count=0))
        nodes = {node.node_id: node for node in asyncio.run(self.storage.list_task_nodes_for_task("task-expand"))}

        self.assertEqual(result.task.status, TaskStatus.COMPLETED)
        self.assertEqual(nodes["probe"].status, NodeStatus.COMPLETED)
        self.assertEqual(nodes["probe_more"].status, NodeStatus.COMPLETED)

    def test_replan_is_rejected_when_dynamic_node_budget_is_exceeded(self) -> None:
        service = self._service(
            executor=FakeExecutor({"cap.fail": error_result(code="boom", message="failed"), "cap.repair": success_result(output_payload={"ok": True})}),
            runtime_replanner=_TooManyNodesReplanner(),
        )
        request = OrchestrationRequest(task_id="task-budget", conversation_id="conv-1", root_message_id="msg-1", user_message="repair it")
        plan = WorkflowPlan(
            task_id="task-budget",
            nodes=(WorkflowNodePlan(node_id="fail", capability_id="cap.fail"),),
            max_replans=1,
            max_dynamic_nodes=1,
        )

        result = asyncio.run(service.execute_request(request, plan, active_task_count=0))
        events = asyncio.run(self.storage.list_events_for_task("task-budget"))

        self.assertEqual(result.task.status, TaskStatus.FAILED)
        self.assertTrue(any(event.event_type == "task.replan_rejected" for event in events))

    def test_required_failure_without_replan_decision_records_terminal_task_failure(self) -> None:
        service = self._service(
            executor=FakeExecutor({"cap.fail": error_result(code="boom", message="failed")}),
            runtime_replanner=_AsyncNoneReplanner(),
        )
        request = OrchestrationRequest(task_id="task-no-replan", conversation_id="conv-1", root_message_id="msg-1", user_message="repair it")
        plan = WorkflowPlan(
            task_id="task-no-replan",
            nodes=(WorkflowNodePlan(node_id="fail", capability_id="cap.fail"),),
            max_replans=1,
            max_dynamic_nodes=1,
        )

        result = asyncio.run(service.execute_request(request, plan, active_task_count=0))
        events = asyncio.run(self.storage.list_events_for_task("task-no-replan"))
        event_types = [event.event_type for event in events]

        self.assertEqual(result.task.status, TaskStatus.FAILED)
        self.assertEqual(result.completion_status, CompletionStatus.FAILED.value)
        self.assertIn("task.replan_available", event_types)
        self.assertIn("task.failed", event_types)
        self.assertGreater(event_types.index("task.failed"), event_types.index("task.replan_available"))

    def test_replan_rejects_dependency_mutation_for_existing_pending_node(self) -> None:
        service = self._service(
            executor=FakeExecutor({
                "cap.probe": success_result(output_payload={"needs_refine": True}),
                "cap.repair": success_result(output_payload={"ok": True}),
            }),
            runtime_replanner=_UnsafePendingMutationReplanner(),
        )
        request = OrchestrationRequest(task_id="task-unsafe", conversation_id="conv-1", root_message_id="msg-1", user_message="answer safely")
        plan = WorkflowPlan(
            task_id="task-unsafe",
            nodes=(
                WorkflowNodePlan(node_id="search", capability_id="cap.probe"),
                WorkflowNodePlan(node_id="answer", capability_id="cap.repair", depends_on=("search",)),
            ),
            max_replans=1,
            max_dynamic_nodes=2,
        )

        result = asyncio.run(service.execute_request(request, plan, active_task_count=0))
        events = asyncio.run(self.storage.list_events_for_task("task-unsafe"))

        self.assertEqual(result.task.status, TaskStatus.FAILED)
        self.assertTrue(any(event.event_type == "task.replan_rejected" and event.payload.get("reason") == "invalid_runtime_plan" for event in events))

    def test_replan_records_node_orphaned_events_for_removed_nonterminal_nodes(self) -> None:
        service = self._service(
            executor=FakeExecutor({"cap.probe": success_result(output_payload={"ok": True}), "cap.repair": success_result(output_payload={"ok": True})}),
            runtime_replanner=_AsyncNoneReplanner(),
        )
        task = Task(
            task_id="task-orphan",
            conversation_id="conv-1",
            root_message_id="msg-1",
            status=TaskStatus.RUNNING,
        )
        kept_node = TaskNode(node_id="kept", task_id="task-orphan", capability_id="cap.probe", status=NodeStatus.COMPLETED)
        stale_a = TaskNode(node_id="stale-a", task_id="task-orphan", capability_id="cap.repair", status=NodeStatus.WAITING_FOR_DEPENDENCY)
        stale_b = TaskNode(node_id="stale-b", task_id="task-orphan", capability_id="cap.repair", status=NodeStatus.PENDING)
        asyncio.run(self.storage.save_task(task))
        asyncio.run(self.storage.save_task_node(kept_node))
        asyncio.run(self.storage.save_task_node(stale_a))
        asyncio.run(self.storage.save_task_node(stale_b))

        request = OrchestrationRequest(task_id="task-orphan", conversation_id="conv-1", root_message_id="msg-1", user_message="replan")
        current_plan = WorkflowPlan(
            task_id="task-orphan",
            nodes=(
                WorkflowNodePlan(node_id="kept", capability_id="cap.probe"),
                WorkflowNodePlan(node_id="stale-b", capability_id="cap.repair", criticality=NodeCriticality.OPTIONAL, metadata={"skill_name": "RepairSkillB"}),
                WorkflowNodePlan(node_id="stale-a", capability_id="cap.repair", criticality=NodeCriticality.OPTIONAL, metadata={"skill_name": "RepairSkillA"}),
            ),
            max_replans=1,
            max_dynamic_nodes=1,
        )
        revised_plan = WorkflowPlan(
            task_id="task-orphan",
            nodes=(WorkflowNodePlan(node_id="kept", capability_id="cap.probe"),),
            max_replans=1,
            max_dynamic_nodes=1,
        )
        applied = asyncio.run(
            service._apply_runtime_replan(
                request=request,
                current_plan=current_plan,
                decision=RuntimeReplanDecision(plan=revised_plan, reason="remove_stale_optional_branch"),
                current_nodes={"kept": kept_node, "stale-a": stale_a, "stale-b": stale_b},
                replan_count=0,
                dynamic_node_count=0,
            )
        )

        self.assertIsNotNone(applied)
        reloaded_stale_a = asyncio.run(self.storage.get_task_node("stale-a"))
        reloaded_stale_b = asyncio.run(self.storage.get_task_node("stale-b"))
        self.assertEqual(reloaded_stale_a.status, NodeStatus.ORPHANED)
        self.assertEqual(reloaded_stale_b.status, NodeStatus.ORPHANED)
        events = asyncio.run(self.storage.list_events_for_task("task-orphan"))
        orphan_events = [event for event in events if event.event_type == "node.orphaned"]
        self.assertEqual([event.node_id for event in orphan_events], ["stale-a", "stale-b"])
        self.assertEqual(orphan_events[0].payload["status"], "orphaned")
        self.assertEqual(orphan_events[0].payload["capability_id"], "cap.repair")
        self.assertEqual(orphan_events[0].payload["skill_name"], "RepairSkillA")
        self.assertEqual(orphan_events[0].payload["reason"], "remove_stale_optional_branch")
        self.assertEqual(orphan_events[1].payload["skill_name"], "RepairSkillB")
        graph_updated = next(event for event in events if event.event_type == "task.graph_updated")
        self.assertEqual(graph_updated.payload["orphaned_node_ids"], ["stale-a", "stale-b"])

    def test_resume_execution_records_node_resuming_before_restart(self) -> None:
        service = self._service(
            executor=FakeExecutor({"cap.repair": success_result(output_payload={"ok": True})}),
            runtime_replanner=_AsyncNoneReplanner(),
        )
        task = Task(
            task_id="task-resume",
            conversation_id="conv-1",
            root_message_id="msg-1",
            status=TaskStatus.RUNNING,
            root_node_id="skill-node",
        )
        interrupted_node = TaskNode(
            node_id="skill-node",
            task_id="task-resume",
            capability_id="cap.repair",
            status=NodeStatus.READY_TO_RESUME,
        )
        asyncio.run(self.storage.save_task(task))
        asyncio.run(self.storage.save_task_node(interrupted_node))

        request = OrchestrationRequest(
            task_id="task-resume",
            conversation_id="conv-1",
            root_message_id="msg-1",
            user_message="resume",
            requested_capability_id="cap.repair",
            metadata={"resume_interrupted_node_id": "skill-node"},
        )
        plan = WorkflowPlan(task_id="task-resume", nodes=(WorkflowNodePlan(node_id="skill-node", capability_id="cap.repair"),))

        result = asyncio.run(service.execute_request(request, plan, active_task_count=0))
        events = asyncio.run(self.storage.list_events_for_task("task-resume"))
        event_types = [event.event_type for event in events]

        self.assertEqual(result.task.status, TaskStatus.COMPLETED)
        self.assertIn("node.resuming", event_types)
        self.assertIn("node.started", event_types)
        self.assertLess(event_types.index("node.resuming"), event_types.index("node.started"))
        resuming_event = next(event for event in events if event.event_type == "node.resuming")
        self.assertEqual(resuming_event.node_id, "skill-node")
        self.assertEqual(resuming_event.payload["status"], "resuming")
        self.assertEqual(resuming_event.payload["capability_id"], "cap.repair")

    def test_resume_execution_preserves_existing_resuming_node_without_duplicate_resuming_event(self) -> None:
        service = self._service(
            executor=FakeExecutor({"cap.repair": success_result(output_payload={"ok": True})}),
            runtime_replanner=_AsyncNoneReplanner(),
        )
        task = Task(
            task_id="task-already-resuming",
            conversation_id="conv-1",
            root_message_id="msg-1",
            status=TaskStatus.RUNNING,
            root_node_id="skill-node",
        )
        interrupted_node = TaskNode(
            node_id="skill-node",
            task_id="task-already-resuming",
            capability_id="cap.repair",
            status=NodeStatus.RESUMING,
        )
        asyncio.run(self.storage.save_task(task))
        asyncio.run(self.storage.save_task_node(interrupted_node))

        request = OrchestrationRequest(
            task_id="task-already-resuming",
            conversation_id="conv-1",
            root_message_id="msg-1",
            user_message="resume",
            requested_capability_id="cap.repair",
            metadata={"resume_interrupted_node_id": "skill-node"},
        )
        plan = WorkflowPlan(task_id="task-already-resuming", nodes=(WorkflowNodePlan(node_id="skill-node", capability_id="cap.repair"),))

        result = asyncio.run(service.execute_request(request, plan, active_task_count=0))
        events = asyncio.run(self.storage.list_events_for_task("task-already-resuming"))

        self.assertEqual(result.task.status, TaskStatus.COMPLETED)
        self.assertEqual([event.event_type for event in events].count("node.resuming"), 0)
        self.assertEqual((asyncio.run(self.storage.get_task_node("skill-node"))).status, NodeStatus.COMPLETED)

    def test_replan_rejects_capability_mutation_for_existing_completed_node(self) -> None:
        service = self._service(
            executor=FakeExecutor({
                "cap.probe": success_result(output_payload={"ok": True}),
                "cap.repair": success_result(output_payload={"ok": True}),
            }),
            runtime_replanner=_UnsafeCompletedMutationReplanner(),
        )
        request = OrchestrationRequest(task_id="task-unsafe-completed", conversation_id="conv-1", root_message_id="msg-1", user_message="answer safely")
        plan = WorkflowPlan(
            task_id="task-unsafe-completed",
            nodes=(WorkflowNodePlan(node_id="probe", capability_id="cap.probe"),),
            max_replans=1,
            max_dynamic_nodes=1,
        )

        result = asyncio.run(service.execute_request(request, plan, active_task_count=0))
        events = asyncio.run(self.storage.list_events_for_task("task-unsafe-completed"))

        self.assertEqual(result.task.status, TaskStatus.FAILED)
        self.assertTrue(any(event.event_type == "task.replan_rejected" and event.payload.get("reason") == "invalid_runtime_plan" for event in events))

    def test_composite_replanner_continues_after_async_none_decision(self) -> None:
        service = self._service(
            executor=FakeExecutor({"cap.fail": error_result(code="boom", message="failed"), "cap.repair": success_result(output_payload={"ok": True})}),
            runtime_replanner=CompositeRuntimeReplanner([_AsyncNoneReplanner(), _SyncDecisionReplanner()]),
        )
        request = OrchestrationRequest(task_id="task-composite", conversation_id="conv-1", root_message_id="msg-1", user_message="repair it")
        plan = WorkflowPlan(
            task_id="task-composite",
            nodes=(WorkflowNodePlan(node_id="fail", capability_id="cap.fail"),),
            max_replans=1,
            max_dynamic_nodes=1,
        )

        result = asyncio.run(service.execute_request(request, plan, active_task_count=0))

        self.assertEqual(result.task.status, TaskStatus.COMPLETED)


if __name__ == "__main__":
    unittest.main()
