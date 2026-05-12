from __future__ import annotations

import unittest

from src.capabilities.main_agent import MainAgentWorkflowProvider
from src.orchestration.models import OrchestrationRequest
from src.orchestration.skill_workflow_provider import SkillWorkflowProvider
from src.orchestration.workflow_router import WorkflowRouter


class WorkflowRouterTest(unittest.TestCase):
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

    def test_legacy_sql_query_id_is_not_special_cased_inside_router(self) -> None:
        router = WorkflowRouter(
            default_provider=MainAgentWorkflowProvider(),
            main_agent_provider=MainAgentWorkflowProvider(),
            skill_provider=SkillWorkflowProvider({"skill.sql_query": "sql-query"}),
        )
        plan = router.build_plan(
            OrchestrationRequest(
                task_id="task-legacy-sql",
                conversation_id="conv-1",
                root_message_id="msg-1",
                user_message="查询数据",
                requested_capability_id="sql_query.query",
            )
        )
        self.assertEqual(plan.metadata["route"], "main_agent")
        self.assertEqual([node.capability_id for node in plan.nodes], ["main_agent.respond"])


if __name__ == "__main__":
    unittest.main()
