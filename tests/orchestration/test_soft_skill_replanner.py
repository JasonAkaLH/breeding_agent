from __future__ import annotations

import unittest
from pathlib import Path

from src.core.enums import NodeStatus
from src.core.models import TaskNode
from src.orchestration.models import CapabilityDescriptor, OrchestrationRequest, WorkflowNodePlan, WorkflowPlan
from src.orchestration.registry import CapabilityRegistry
from src.orchestration.runtime_replanner import RuntimeReplanContext
from src.orchestration.skill_workflow_provider import SkillWorkflowProvider
from src.orchestration.soft_skill_replanner import SoftSkillBindingReplanner
from src.orchestration.completion_policy import CompletionStatus
from src.integrations.agent_skills import SkillManifest


class SoftSkillBindingReplannerTest(unittest.TestCase):
    def test_execute_signal_expands_only_the_bound_skill(self) -> None:
        registry = CapabilityRegistry()
        registry.register(CapabilityDescriptor("main_agent.respond", "main", "main"))
        registry.register(CapabilityDescriptor("skill.demo", "demo", "demo", kind="skill", source="skill"))
        manifest = SkillManifest(
            name="demo-skill",
            description="demo",
            triggers=(),
            body="body",
            source_path=Path("skill/demo/SKILL.md"),
            metadata={"execution": {"mode": "platform_service", "handler": "skill.demo.handler", "answer_mode": "requires_finalizer"}},
        )
        provider = SkillWorkflowProvider(
            {"skill.demo": "demo-skill"},
            skill_manifest_resolver=lambda capability_id, _revision: manifest if capability_id == "skill.demo" else None,
        )
        replanner = SoftSkillBindingReplanner(
            capability_registry=registry,
            macro_providers={},
            macro_provider_resolver=lambda capability_id: provider if capability_id == "skill.demo" else None,
            active_skill_revision_resolver=lambda capability_id: "skillrev-1",
        )
        plan = WorkflowPlan(
            task_id="task-1",
            nodes=(WorkflowNodePlan("task-1:main_agent.respond", "main_agent.respond"),),
            max_replans=1,
            max_dynamic_nodes=4,
        )
        context = RuntimeReplanContext(
            request=OrchestrationRequest(
                task_id="task-1",
                conversation_id="conv-1",
                root_message_id="msg-1",
                user_message="请执行",
                requested_capability_id="main_agent.respond",
                metadata={"soft_skill_binding": {"capability_id": "skill.demo", "skill_bundle_revision": "skillrev-1"}},
            ),
            plan=plan,
            nodes={"task-1:main_agent.respond": TaskNode("task-1:main_agent.respond", "task-1", "main_agent.respond", status=NodeStatus.COMPLETED)},
            node_outputs={
                "task-1:main_agent.respond": {
                    "soft_skill_decision": {"decision": "execute", "target_capability_id": "skill.demo"}
                }
            },
            completion_status=CompletionStatus.RUNNING,
            replan_count=0,
            dynamic_node_count=0,
            unresolved_interrupt=False,
        )

        decision = replanner.build_replan(context)

        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertEqual(decision.reason, "soft_skill_execute")
        self.assertIn("skill.demo", [node.capability_id for node in decision.plan.nodes])
        skill_node = next(node for node in decision.plan.nodes if node.capability_id == "skill.demo")
        self.assertIn("task-1:main_agent.respond", skill_node.depends_on)

    def test_target_mismatch_is_rejected(self) -> None:
        registry = CapabilityRegistry()
        registry.register(CapabilityDescriptor("main_agent.respond", "main", "main"))
        registry.register(CapabilityDescriptor("skill.demo", "demo", "demo", kind="skill", source="skill"))
        registry.register(CapabilityDescriptor("skill.other", "other", "other", kind="skill", source="skill"))
        replanner = SoftSkillBindingReplanner(
            capability_registry=registry,
            macro_providers={},
            macro_provider_resolver=lambda _capability_id: SkillWorkflowProvider({"skill.demo": "demo-skill"}),
        )
        plan = WorkflowPlan(
            task_id="task-1",
            nodes=(WorkflowNodePlan("main", "main_agent.respond"),),
            max_replans=1,
            max_dynamic_nodes=4,
        )
        context = RuntimeReplanContext(
            request=OrchestrationRequest(
                task_id="task-1",
                conversation_id="conv-1",
                root_message_id="msg-1",
                user_message="请执行",
                metadata={"soft_skill_binding": {"capability_id": "skill.demo"}},
            ),
            plan=plan,
            nodes={"main": TaskNode("main", "task-1", "main_agent.respond", status=NodeStatus.COMPLETED)},
            node_outputs={"main": {"soft_skill_decision": {"decision": "execute", "target_capability_id": "skill.other"}}},
            completion_status=CompletionStatus.RUNNING,
            replan_count=0,
            dynamic_node_count=0,
            unresolved_interrupt=False,
        )

        self.assertIsNone(replanner.build_replan(context))

    def test_revision_drift_is_rejected_before_internal_skill_expansion(self) -> None:
        registry = CapabilityRegistry()
        registry.register(CapabilityDescriptor("main_agent.respond", "main", "main"))
        registry.register(CapabilityDescriptor("skill.demo", "demo", "demo", kind="skill", source="skill"))
        replanner = SoftSkillBindingReplanner(
            capability_registry=registry,
            macro_providers={},
            macro_provider_resolver=lambda _capability_id: SkillWorkflowProvider({"skill.demo": "demo-skill"}),
            active_skill_revision_resolver=lambda _capability_id: "skillrev-current",
        )
        plan = WorkflowPlan(
            task_id="task-1",
            nodes=(WorkflowNodePlan("main", "main_agent.respond"),),
            max_replans=1,
            max_dynamic_nodes=4,
        )
        context = RuntimeReplanContext(
            request=OrchestrationRequest(
                task_id="task-1",
                conversation_id="conv-1",
                root_message_id="msg-1",
                user_message="请执行",
                metadata={"soft_skill_binding": {"capability_id": "skill.demo", "skill_bundle_revision": "skillrev-old"}},
            ),
            plan=plan,
            nodes={"main": TaskNode("main", "task-1", "main_agent.respond", status=NodeStatus.COMPLETED)},
            node_outputs={"main": {"soft_skill_decision": {"decision": "execute", "target_capability_id": "skill.demo"}}},
            completion_status=CompletionStatus.RUNNING,
            replan_count=0,
            dynamic_node_count=0,
            unresolved_interrupt=False,
        )

        self.assertIsNone(replanner.build_replan(context))


if __name__ == "__main__":
    unittest.main()
