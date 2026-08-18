from __future__ import annotations

import unittest

from src.orchestration.models import WorkflowNodePlan, WorkflowPlan
from src.orchestration.planner_node_identity import (
    PlannerNodeIdentityError,
    PlannerNodeIdentityMap,
    validate_canonical_model_node_identity,
)


class PlannerNodeIdentityMapTest(unittest.TestCase):
    def test_canonicalizes_nodes_and_dependencies_with_audit_metadata(self) -> None:
        plan = WorkflowPlan(
            task_id="task-a",
            nodes=(
                WorkflowNodePlan(node_id="Query Data", capability_id="skill.query"),
                WorkflowNodePlan(
                    node_id="answer_user",
                    capability_id="main_agent.respond",
                    depends_on=("Query Data",),
                ),
            ),
        )

        canonical = PlannerNodeIdentityMap(task_id="task-a", planning_epoch="p0").canonicalize(plan)

        query, answer = canonical.nodes
        self.assertRegex(query.node_id, r"^task-a:plan:v1:p0:query-data:[0-9a-f]{20}$")
        self.assertRegex(answer.node_id, r"^task-a:plan:v1:p0:answer_user:[0-9a-f]{20}$")
        self.assertEqual(answer.depends_on, (query.node_id,))
        self.assertEqual(query.metadata["identity_origin"], "model")
        self.assertEqual(query.metadata["identity_version"], "v1")
        self.assertEqual(query.metadata["planning_epoch"], "p0")
        self.assertEqual(query.metadata["planner_node_key"], "query-data")

    def test_same_local_keys_are_isolated_by_task_and_epoch(self) -> None:
        plan_a = WorkflowPlan(
            task_id="task-a",
            nodes=(WorkflowNodePlan(node_id="n1", capability_id="main_agent.respond"),),
        )
        plan_b = WorkflowPlan(
            task_id="task-b",
            nodes=(WorkflowNodePlan(node_id="n1", capability_id="main_agent.respond"),),
        )

        initial_a = PlannerNodeIdentityMap(task_id="task-a", planning_epoch="p0").canonicalize(plan_a)
        retry_a = PlannerNodeIdentityMap(task_id="task-a", planning_epoch="p0").canonicalize(plan_a)
        replan_a = PlannerNodeIdentityMap(task_id="task-a", planning_epoch="r1").canonicalize(plan_a)
        initial_b = PlannerNodeIdentityMap(task_id="task-b", planning_epoch="p0").canonicalize(plan_b)

        self.assertEqual(initial_a.nodes[0].node_id, retry_a.nodes[0].node_id)
        self.assertNotEqual(initial_a.nodes[0].node_id, replan_a.nodes[0].node_id)
        self.assertNotEqual(initial_a.nodes[0].node_id, initial_b.nodes[0].node_id)

    def test_rejects_duplicate_dangling_and_untrusted_keys(self) -> None:
        cases = (
            (
                WorkflowPlan(
                    task_id="task-a",
                    nodes=(
                        WorkflowNodePlan(node_id="dup", capability_id="cap.a"),
                        WorkflowNodePlan(node_id="dup", capability_id="cap.b"),
                    ),
                ),
                "duplicate planner node key",
            ),
            (
                WorkflowPlan(
                    task_id="task-a",
                    nodes=(
                        WorkflowNodePlan(
                            node_id="answer",
                            capability_id="main_agent.respond",
                            depends_on=("missing",),
                        ),
                    ),
                ),
                "unknown planner node dependency",
            ),
            (
                WorkflowPlan(
                    task_id="task-a",
                    nodes=(WorkflowNodePlan(node_id="bad\nkey", capability_id="cap.a"),),
                ),
                "control character",
            ),
            (
                WorkflowPlan(
                    task_id="task-a",
                    nodes=(WorkflowNodePlan(node_id="界" * 86, capability_id="cap.a"),),
                ),
                "256 UTF-8 bytes",
            ),
        )

        for plan, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(PlannerNodeIdentityError, message):
                PlannerNodeIdentityMap(task_id="task-a", planning_epoch="p0").canonicalize(plan)

    def test_rejects_canonical_digest_collision(self) -> None:
        plan = WorkflowPlan(
            task_id="task-a",
            nodes=(
                WorkflowNodePlan(node_id="A B", capability_id="cap.a"),
                WorkflowNodePlan(node_id="A@B", capability_id="cap.b"),
            ),
        )

        with self.assertRaisesRegex(PlannerNodeIdentityError, "canonical node identity collision"):
            PlannerNodeIdentityMap(
                task_id="task-a",
                planning_epoch="p0",
                digest_factory=lambda _payload: "0" * 20,
            ).canonicalize(plan)

    def test_rejects_plan_from_another_task(self) -> None:
        plan = WorkflowPlan(
            task_id="task-b",
            nodes=(WorkflowNodePlan(node_id="n1", capability_id="cap.a"),),
        )

        with self.assertRaisesRegex(PlannerNodeIdentityError, "task_id mismatch"):
            PlannerNodeIdentityMap(task_id="task-a", planning_epoch="p0").canonicalize(plan)

    def test_persistence_guard_only_rejects_forged_model_origin_nodes(self) -> None:
        system_node = WorkflowNodePlan(node_id="answer_user", capability_id="main_agent.respond")
        validate_canonical_model_node_identity(system_node, task_id="task-a")

        canonical = PlannerNodeIdentityMap(task_id="task-a", planning_epoch="p0").canonicalize(
            WorkflowPlan(task_id="task-a", nodes=(system_node,))
        ).nodes[0]
        validate_canonical_model_node_identity(canonical, task_id="task-a")

        with self.assertRaisesRegex(PlannerNodeIdentityError, "does not match task"):
            validate_canonical_model_node_identity(canonical, task_id="task-b")
        with self.assertRaisesRegex(PlannerNodeIdentityError, "identity_version"):
            validate_canonical_model_node_identity(
                WorkflowNodePlan(
                    node_id=canonical.node_id,
                    capability_id=canonical.capability_id,
                    metadata={**canonical.metadata, "identity_version": "v2"},
                ),
                task_id="task-a",
            )
