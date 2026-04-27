from __future__ import annotations

import unittest

from src.orchestration.models import CapabilityDescriptor, WorkflowNodePlan, WorkflowPlan
from src.orchestration.registry import CapabilityRegistry
from src.orchestration.workflow_plan_validator import WorkflowPlanValidationError, WorkflowPlanValidator


class WorkflowPlanValidatorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = CapabilityRegistry()
        self.registry.register(CapabilityDescriptor(capability_id="sql_query.query", name="SQLQuery", description="public SQLQuery macro"))
        self.registry.register(CapabilityDescriptor(capability_id="main_agent.respond", name="respond", description="main response"))
        self.registry.register(CapabilityDescriptor(capability_id="sql_query.sql_generate", name="sql_generate", description="internal", public=False))

    def test_public_planner_plan_accepts_only_public_capabilities(self) -> None:
        plan = WorkflowPlan(
            task_id="task-1",
            nodes=(
                WorkflowNodePlan(node_id="query_data", capability_id="sql_query.query", input_payload={"user_question": "查询龙粳33"}),
                WorkflowNodePlan(node_id="answer_user", capability_id="main_agent.respond", depends_on=("query_data",)),
            ),
        )

        WorkflowPlanValidator(self.registry, public_only=True).validate(plan)

    def test_public_planner_plan_rejects_internal_sql_query_node(self) -> None:
        plan = WorkflowPlan(
            task_id="task-1",
            nodes=(WorkflowNodePlan(node_id="generate_sql", capability_id="sql_query.sql_generate"),),
        )

        with self.assertRaisesRegex(WorkflowPlanValidationError, "not public"):
            WorkflowPlanValidator(self.registry, public_only=True).validate(plan)

    def test_rejects_missing_dependency(self) -> None:
        plan = WorkflowPlan(
            task_id="task-1",
            nodes=(WorkflowNodePlan(node_id="answer_user", capability_id="main_agent.respond", depends_on=("missing",)),),
        )

        with self.assertRaisesRegex(WorkflowPlanValidationError, "unknown dependency"):
            WorkflowPlanValidator(self.registry).validate(plan)

    def test_rejects_cycles(self) -> None:
        plan = WorkflowPlan(
            task_id="task-1",
            nodes=(
                WorkflowNodePlan(node_id="a", capability_id="main_agent.respond", depends_on=("b",)),
                WorkflowNodePlan(node_id="b", capability_id="sql_query.query", depends_on=("a",)),
            ),
        )

        with self.assertRaisesRegex(WorkflowPlanValidationError, "cycle"):
            WorkflowPlanValidator(self.registry).validate(plan)

    def test_rejects_non_json_serializable_input_payload(self) -> None:
        plan = WorkflowPlan(
            task_id="task-1",
            nodes=(WorkflowNodePlan(node_id="answer_user", capability_id="main_agent.respond", input_payload={"bad": object()}),),
        )

        with self.assertRaisesRegex(WorkflowPlanValidationError, "JSON serializable"):
            WorkflowPlanValidator(self.registry).validate(plan)


if __name__ == "__main__":
    unittest.main()
