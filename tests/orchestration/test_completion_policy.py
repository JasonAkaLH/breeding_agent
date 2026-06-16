from __future__ import annotations

import unittest

from src.core.enums import NodeCriticality, NodeStatus
from src.orchestration.completion_policy import CompletionPolicy, CompletionStatus
from src.orchestration.models import WorkflowNodePlan, WorkflowPlan


class CompletionPolicyTest(unittest.TestCase):
    def test_required_nodes_must_complete(self) -> None:
        policy = CompletionPolicy()
        plan = WorkflowPlan(
            task_id="task-1",
            nodes=(
                WorkflowNodePlan(node_id="node-required", capability_id="cap.route"),
                WorkflowNodePlan(node_id="node-optional", capability_id="cap.extra", criticality=NodeCriticality.OPTIONAL),
            ),
        )

        status = policy.evaluate(
            plan,
            {
                "node-required": NodeStatus.COMPLETED,
                "node-optional": NodeStatus.FAILED,
            },
        )

        self.assertEqual(status, CompletionStatus.COMPLETED)

    def test_required_failure_exposes_replan_entry_when_budget_available(self) -> None:
        policy = CompletionPolicy()
        plan = WorkflowPlan(
            task_id="task-1",
            nodes=(WorkflowNodePlan(node_id="node-required", capability_id="cap.route"),),
            max_replans=1,
        )

        status = policy.evaluate(
            plan,
            {"node-required": NodeStatus.FAILED},
            replan_count=0,
        )

        self.assertEqual(status, CompletionStatus.REPLAN_AVAILABLE)

    def test_required_failure_becomes_failed_when_replan_budget_exhausted(self) -> None:
        policy = CompletionPolicy()
        plan = WorkflowPlan(
            task_id="task-1",
            nodes=(WorkflowNodePlan(node_id="node-required", capability_id="cap.route"),),
            max_replans=0,
        )

        status = policy.evaluate(plan, {"node-required": NodeStatus.FAILED}, replan_count=0)
        self.assertEqual(status, CompletionStatus.FAILED)
