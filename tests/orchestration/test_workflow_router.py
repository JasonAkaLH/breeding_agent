from __future__ import annotations

import unittest

from src.capabilities.main_agent import MainAgentWorkflowProvider
from src.capabilities.mcp_dispatch import MCPDispatchWorkflowProvider
from src.orchestration.models import OrchestrationRequest
from src.orchestration.skill_workflow_provider import SkillWorkflowProvider
from src.orchestration.workflow_router import WorkflowRouter


class WorkflowRouterTest(unittest.TestCase):
    def test_explicit_mcp_dispatch_routes_to_resume_provider(self) -> None:
        router = WorkflowRouter(
            default_provider=MainAgentWorkflowProvider(),
            main_agent_provider=MainAgentWorkflowProvider(),
            mcp_provider=MCPDispatchWorkflowProvider(),
        )
        plan = router.build_plan(
            OrchestrationRequest(
                task_id="task-mcp-resume",
                conversation_id="conv-1",
                root_message_id="msg-1",
                user_message="继续查询",
                requested_capability_id="mcp.dispatch",
                metadata={"mcp_dispatch_server_id": "server-1"},
            )
        )

        self.assertEqual(plan.metadata["route"], "mcp_dispatch")
        self.assertEqual([node.capability_id for node in plan.nodes], ["mcp.dispatch", "main_agent.respond"])
        self.assertEqual(plan.nodes[1].depends_on, (plan.nodes[0].node_id,))

    def test_top_level_skill_capability_routes_to_skill_provider(self) -> None:
        router = WorkflowRouter(
            default_provider=MainAgentWorkflowProvider(),
            main_agent_provider=MainAgentWorkflowProvider(),
            skill_provider=SkillWorkflowProvider({"skill.mini_breedstat_rcbd": "mini-breedstat-rcbd"}),
        )
        plan = router.build_plan(
            OrchestrationRequest(
                task_id="task-skill-route",
                conversation_id="conv-1",
                root_message_id="msg-1",
                user_message="做随机区组设计",
                requested_capability_id="skill.mini_breedstat_rcbd",
            )
        )
        self.assertEqual(plan.metadata["route"], "skill")
        self.assertEqual([node.capability_id for node in plan.nodes], ["main_agent.respond"])
        self.assertEqual(plan.nodes[0].metadata["forced_skill_name"], "mini-breedstat-rcbd")
        self.assertEqual(plan.nodes[0].metadata["forced_skill_source"], "explicit_request")

    def test_unknown_non_skill_id_routes_to_main_agent(self) -> None:
        router = WorkflowRouter(
            default_provider=MainAgentWorkflowProvider(),
            main_agent_provider=MainAgentWorkflowProvider(),
            skill_provider=SkillWorkflowProvider({"skill.generic_data_lookup": "generic-data-lookup"}),
        )
        plan = router.build_plan(
            OrchestrationRequest(
                task_id="task-unknown-route",
                conversation_id="conv-1",
                root_message_id="msg-1",
                user_message="查询数据",
                requested_capability_id="unknown.capability",
            )
        )
        self.assertEqual(plan.metadata["route"], "main_agent")
        self.assertEqual([node.capability_id for node in plan.nodes], ["main_agent.respond"])


if __name__ == "__main__":
    unittest.main()
