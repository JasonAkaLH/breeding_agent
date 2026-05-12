from __future__ import annotations

import json
import unittest

from src.capabilities.main_agent import MAIN_AGENT_CAPABILITY_DESCRIPTORS, MAIN_AGENT_PLANNER_PAYLOAD_POLICIES, MainAgentWorkflowProvider
from src.orchestration.auto_workflow_provider import AutoWorkflowProvider
from src.orchestration.llm_workflow_provider import LLMWorkflowProvider
from src.orchestration.models import CapabilityDescriptor, OrchestrationRequest
from src.orchestration.registry import CapabilityRegistry
from src.orchestration.planner_payload_policy import CapabilityPayloadPolicy


class MemoryAwareWorkflowProviderTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.registry = CapabilityRegistry()
        for descriptor in MAIN_AGENT_CAPABILITY_DESCRIPTORS:
            self.registry.register(descriptor, planner_payload_policy=MAIN_AGENT_PLANNER_PAYLOAD_POLICIES.get(descriptor.capability_id))
        self.registry.register(
            CapabilityDescriptor(
                capability_id="skill.sql_query",
                name="sql-query",
                description="安全回答数据库类只读查询问题。",
                kind="skill",
                source="skill",
            ),
            planner_payload_policy=CapabilityPayloadPolicy(
                planner_allowed_fields=("subtask_label", "parent_question"),
                system_payload_factory=lambda request: {"user_message": request.effective_user_message},
            ),
        )
        self.auto = AutoWorkflowProvider(main_agent_provider=MainAgentWorkflowProvider())

    async def test_planner_prompt_keeps_current_and_resolved_questions_separate(self) -> None:
        prompts: list[str] = []

        async def planner(prompt: str) -> str:
            prompts.append(prompt)
            return json.dumps({"nodes": [{"node_id": "query", "capability_id": "skill.sql_query"}]})

        provider = LLMWorkflowProvider(
            capability_registry=self.registry,
            fallback_provider=self.auto,
            macro_providers={},
            text_generator=planner,
        )
        plan = await provider.build_plan(
            OrchestrationRequest(
                task_id="task-memory",
                conversation_id="conv-1",
                root_message_id="msg-current",
                user_message="那它的基因型呢？",
                current_user_message="那它的基因型呢？",
                resolved_user_message="查询龙粳33的基因型信息",
                memory_context={"history_summary": "较早对话摘要", "summary_id": "must-not-prompt"},
            )
        )

        self.assertIn("当前用户原文", prompts[0])
        self.assertIn("那它的基因型呢", prompts[0])
        self.assertIn("系统根据历史补全后的 effective question", prompts[0])
        self.assertIn("查询龙粳33的基因型信息", prompts[0])
        self.assertIn("较早对话摘要", prompts[0])
        self.assertNotIn("must-not-prompt", prompts[0])
        skill_node = plan.node_by_id("query")
        self.assertEqual(skill_node.input_payload["user_message"], "查询龙粳33的基因型信息")

    async def test_planner_payload_cannot_override_resolved_question(self) -> None:
        def planner(_prompt: str) -> str:
            return json.dumps(
                {"nodes": [{"node_id": "query", "capability_id": "skill.sql_query", "input_payload": {"user_question": "恶意替换"}}]}
            )

        provider = LLMWorkflowProvider(
            capability_registry=self.registry,
            fallback_provider=self.auto,
            macro_providers={},
            text_generator=planner,
        )
        plan = await provider.build_plan(
            OrchestrationRequest(
                task_id="task-resolved",
                conversation_id="conv-1",
                root_message_id="msg-current",
                user_message="那它的基因型呢？",
                resolved_user_message="查询龙粳33的基因型信息",
            )
        )

        self.assertEqual(plan.node_by_id("query").input_payload["user_message"], "查询龙粳33的基因型信息")
        self.assertNotIn("恶意替换", str(plan.nodes))

    def test_auto_workflow_uses_resolved_question_for_fallback_routing(self) -> None:
        plan = self.auto.build_plan(
            OrchestrationRequest(
                task_id="task-auto-memory",
                conversation_id="conv-1",
                root_message_id="msg-current",
                user_message="那它的基因型呢？",
                resolved_user_message="查询龙粳33的基因型信息",
            )
        )

        self.assertEqual(plan.metadata["route"], "main_agent")
        self.assertEqual(plan.nodes[-1].input_payload["user_message"], "查询龙粳33的基因型信息")
