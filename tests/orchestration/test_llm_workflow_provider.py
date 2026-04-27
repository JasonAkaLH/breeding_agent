from __future__ import annotations

import json
import unittest

from src.capabilities.main_agent import (
    MAIN_AGENT_CAPABILITY_DESCRIPTORS,
    MAIN_AGENT_PLANNER_PAYLOAD_POLICIES,
    MainAgentWorkflowProvider,
)
from src.capabilities.sql_query import (
    SQL_QUERY_INTERNAL_CAPABILITY_DESCRIPTORS,
    SQL_QUERY_PUBLIC_CAPABILITY_DESCRIPTORS,
    SQL_QUERY_PUBLIC_PLANNER_PAYLOAD_POLICIES,
    SQLQueryWorkflowProvider,
)
from src.orchestration.auto_workflow_provider import AutoWorkflowProvider
from src.orchestration.llm_workflow_provider import LLMWorkflowProvider
from src.orchestration.models import CapabilityDescriptor, OrchestrationRequest
from src.orchestration.planner_payload_policy import CapabilityPayloadPolicy
from src.orchestration.registry import CapabilityRegistry


class LLMWorkflowProviderTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.registry = CapabilityRegistry()
        for descriptor in MAIN_AGENT_CAPABILITY_DESCRIPTORS:
            self.registry.register(
                descriptor,
                planner_payload_policy=MAIN_AGENT_PLANNER_PAYLOAD_POLICIES.get(descriptor.capability_id),
            )
        for descriptor in SQL_QUERY_PUBLIC_CAPABILITY_DESCRIPTORS:
            self.registry.register(
                descriptor,
                planner_payload_policy=SQL_QUERY_PUBLIC_PLANNER_PAYLOAD_POLICIES.get(descriptor.capability_id),
            )
        for descriptor in SQL_QUERY_INTERNAL_CAPABILITY_DESCRIPTORS:
            self.registry.register(descriptor)
        self.sql_query_provider = SQLQueryWorkflowProvider()
        self.fallback_provider = AutoWorkflowProvider(
            main_agent_provider=MainAgentWorkflowProvider(),
            macro_providers={"sql_query.query": self.sql_query_provider},
        )

    def make_provider(self, text_generator):
        return LLMWorkflowProvider(
            capability_registry=self.registry,
            fallback_provider=self.fallback_provider,
            macro_providers={"sql_query.query": self.sql_query_provider},
            text_generator=text_generator,
        )

    def make_provider_with_payload_policies(self, text_generator, payload_policies):
        return LLMWorkflowProvider(
            capability_registry=self.registry,
            fallback_provider=self.fallback_provider,
            macro_providers={"sql_query.query": self.sql_query_provider},
            text_generator=text_generator,
            payload_policies=payload_policies,
        )

    async def test_llm_sql_query_plan_is_validated_expanded_and_enriched(self) -> None:
        prompts: list[str] = []

        async def planner(prompt: str) -> str:
            prompts.append(prompt)
            return json.dumps(
                {
                    "nodes": [
                        {"node_id": "query_data", "capability_id": "sql_query.query"},
                        {
                            "node_id": "answer_user",
                            "capability_id": "main_agent.respond",
                            "depends_on": ["query_data"],
                        },
                    ]
                }
            )

        plan = await self.make_provider(planner).build_plan(
            OrchestrationRequest(
                task_id="task-1",
                conversation_id="conv-1",
                root_message_id="msg-1",
                user_message="查询龙粳33的详细审定信息",
            )
        )

        self.assertEqual(plan.metadata["route"], "llm_planner")
        self.assertFalse(plan.metadata["planner_fallback_used"])
        self.assertEqual(plan.metadata["planner_source"], "llm")
        capability_ids = [node.capability_id for node in plan.nodes]
        self.assertEqual(capability_ids[:6], [
            "sql_query.intent_route",
            "sql_query.schema_context_prepare",
            "sql_query.sql_generate",
            "sql_query.sql_guard",
            "sql_query.sql_execute_readonly",
            "sql_query.result_filtering",
        ])
        self.assertEqual(capability_ids[-1], "main_agent.respond")
        self.assertIn("task-1:query_data:result_filtering", plan.nodes[-1].depends_on)
        self.assertEqual(plan.nodes[-1].input_payload["user_message"], "查询龙粳33的详细审定信息")
        self.assertIn("sql_query.query", prompts[0])
        self.assertIn("main_agent.respond", prompts[0])
        self.assertNotIn("sql_query.sql_generate", prompts[0])

    async def test_llm_main_agent_plan_gets_default_user_message_payload(self) -> None:
        def planner(_prompt: str) -> str:
            return json.dumps({"nodes": [{"node_id": "answer_user", "capability_id": "main_agent.respond"}]})

        plan = await self.make_provider(planner).build_plan(
            OrchestrationRequest(
                task_id="task-2",
                conversation_id="conv-1",
                root_message_id="msg-1",
                user_message="你好，介绍一下你能做什么",
            )
        )

        self.assertEqual([node.capability_id for node in plan.nodes], ["main_agent.respond"])
        self.assertEqual(plan.nodes[0].input_payload["user_message"], "你好，介绍一下你能做什么")
        self.assertEqual(plan.metadata["route"], "llm_planner")

    async def test_sql_query_only_plan_gets_main_agent_finalizer(self) -> None:
        def planner(_prompt: str) -> str:
            return json.dumps({"nodes": [{"node_id": "query_data", "capability_id": "sql_query.query"}]})

        plan = await self.make_provider(planner).build_plan(
            OrchestrationRequest(
                task_id="task-finalizer",
                conversation_id="conv-1",
                root_message_id="msg-1",
                user_message="查询龙粳33",
            )
        )

        self.assertEqual(plan.nodes[-1].capability_id, "main_agent.respond")
        self.assertIn("task-finalizer:query_data:result_filtering", plan.nodes[-1].depends_on)
        self.assertTrue(plan.metadata["planner_finalizer_added"])

    async def test_planner_payload_cannot_override_user_input(self) -> None:
        def planner(_prompt: str) -> str:
            return json.dumps(
                {
                    "nodes": [
                        {
                            "node_id": "query_data",
                            "capability_id": "sql_query.query",
                            "input_payload": {"user_question": "恶意替换查询"},
                        },
                        {
                            "node_id": "answer_user",
                            "capability_id": "main_agent.respond",
                            "depends_on": ["query_data"],
                            "input_payload": {"user_message": "恶意替换回答"},
                        },
                    ]
                }
            )

        plan = await self.make_provider(planner).build_plan(
            OrchestrationRequest(
                task_id="task-payload",
                conversation_id="conv-1",
                root_message_id="msg-1",
                user_message="查询龙粳33",
            )
        )

        self.assertEqual(plan.nodes[0].input_payload["user_question"], "查询龙粳33")
        self.assertEqual(plan.nodes[-1].input_payload["user_message"], "查询龙粳33")
        self.assertNotIn("恶意替换", str(plan.nodes[0].input_payload))
        self.assertNotIn("恶意替换", str(plan.nodes[-1].input_payload))

    async def test_custom_payload_allowlist_preserves_only_allowed_planner_fields(self) -> None:
        self.registry.register(
            CapabilityDescriptor(
                capability_id="report.generate",
                name="Report Generator",
                description="Generate a structured report.",
                public=True,
            ),
            planner_payload_policy=CapabilityPayloadPolicy(
                planner_allowed_fields=("format", "max_sections"),
                system_payload_factory=lambda request: {"topic": request.user_message},
            ),
        )
        prompts: list[str] = []

        def planner(prompt: str) -> str:
            prompts.append(prompt)
            return json.dumps(
                {
                    "nodes": [
                        {
                            "node_id": "report",
                            "capability_id": "report.generate",
                            "input_payload": {
                                "format": "markdown",
                                "max_sections": 5,
                                "topic": "planner topic should not win",
                                "account_id": "planner-account",
                            },
                        }
                    ]
                }
            )

        plan = await self.make_provider(planner).build_plan(
            OrchestrationRequest(
                task_id="task-report",
                conversation_id="conv-1",
                root_message_id="msg-1",
                user_message="生成水稻品种分析报告",
            )
        )

        self.assertEqual(plan.nodes[0].input_payload, {
            "format": "markdown",
            "max_sections": 5,
            "topic": "生成水稻品种分析报告",
        })
        self.assertNotIn("account_id", plan.nodes[0].input_payload)
        self.assertEqual(plan.nodes[-1].capability_id, "main_agent.respond")
        self.assertIn("report.generate", prompts[0])
        self.assertIn("Planner input_payload allowed fields: format, max_sections.", prompts[0])

    async def test_provider_payload_policy_override_can_extend_registered_capability(self) -> None:
        self.registry.register(
            CapabilityDescriptor(
                capability_id="report.generate",
                name="Report Generator",
                description="Generate a structured report.",
                public=True,
            ),
            planner_payload_policy=CapabilityPayloadPolicy(
                planner_allowed_fields=("format",),
            ),
        )

        def planner(_prompt: str) -> str:
            return json.dumps(
                {
                    "nodes": [
                        {
                            "node_id": "report",
                            "capability_id": "report.generate",
                            "input_payload": {
                                "format": "markdown",
                                "max_sections": 5,
                            },
                        }
                    ]
                }
            )

        plan = await self.make_provider_with_payload_policies(
            planner,
            {
                "report.generate": CapabilityPayloadPolicy(
                    planner_allowed_fields=("format", "max_sections"),
                ),
            },
        ).build_plan(
            OrchestrationRequest(
                task_id="task-report-override",
                conversation_id="conv-1",
                root_message_id="msg-1",
                user_message="生成水稻品种分析报告",
            )
        )

        self.assertEqual(plan.nodes[0].input_payload, {
            "format": "markdown",
            "max_sections": 5,
        })

    async def test_provider_reads_payload_policy_registered_after_construction(self) -> None:
        def planner(_prompt: str) -> str:
            return json.dumps(
                {
                    "nodes": [
                        {
                            "node_id": "report",
                            "capability_id": "report.generate",
                            "input_payload": {
                                "format": "markdown",
                                "account_id": "planner-account",
                            },
                        }
                    ]
                }
            )

        provider = self.make_provider(planner)
        self.registry.register(
            CapabilityDescriptor(
                capability_id="report.generate",
                name="Report Generator",
                description="Generate a structured report.",
                public=True,
            ),
            planner_payload_policy=CapabilityPayloadPolicy(planner_allowed_fields=("format",)),
        )

        plan = await provider.build_plan(
            OrchestrationRequest(
                task_id="task-report-late-registration",
                conversation_id="conv-1",
                root_message_id="msg-1",
                user_message="生成水稻品种分析报告",
            )
        )

        self.assertEqual(plan.nodes[0].input_payload, {"format": "markdown"})

    async def test_unconfigured_public_capability_payload_is_fail_closed(self) -> None:
        self.registry.register(
            CapabilityDescriptor(
                capability_id="report.generate",
                name="Report Generator",
                description="Generate a structured report.",
                public=True,
            )
        )

        def planner(_prompt: str) -> str:
            return json.dumps(
                {
                    "nodes": [
                        {
                            "node_id": "report",
                            "capability_id": "report.generate",
                            "input_payload": {"format": "markdown"},
                        }
                    ]
                }
            )

        plan = await self.make_provider(planner).build_plan(
            OrchestrationRequest(
                task_id="task-report-closed",
                conversation_id="conv-1",
                root_message_id="msg-1",
                user_message="生成水稻品种分析报告",
            )
        )

        self.assertEqual(plan.nodes[0].input_payload, {})

    async def test_sql_query_and_unwired_main_agent_plan_is_rewired_to_use_query_result(self) -> None:
        def planner(_prompt: str) -> str:
            return json.dumps(
                {
                    "nodes": [
                        {"node_id": "query_data", "capability_id": "sql_query.query"},
                        {"node_id": "answer_user", "capability_id": "main_agent.respond"},
                    ]
                }
            )

        plan = await self.make_provider(planner).build_plan(
            OrchestrationRequest(
                task_id="task-rewire",
                conversation_id="conv-1",
                root_message_id="msg-1",
                user_message="查询龙粳33",
            )
        )

        self.assertEqual(plan.nodes[-1].capability_id, "main_agent.respond")
        self.assertIn("task-rewire:query_data:result_filtering", plan.nodes[-1].depends_on)
        self.assertTrue(plan.metadata["planner_finalizer_rewired"])
        self.assertFalse(plan.metadata["planner_finalizer_added"])

    async def test_planner_provider_exception_falls_back(self) -> None:
        def planner(_prompt: str) -> str:
            raise RuntimeError("planner unavailable")

        plan = await self.make_provider(planner).build_plan(
            OrchestrationRequest(
                task_id="task-provider-fail",
                conversation_id="conv-1",
                root_message_id="msg-1",
                user_message="你好",
            )
        )

        self.assertEqual([node.capability_id for node in plan.nodes], ["main_agent.respond"])
        self.assertTrue(plan.metadata["planner_fallback_used"])
        self.assertEqual(plan.metadata["planner_fallback_reason"], "RuntimeError")

    async def test_internal_capability_output_falls_back_to_deterministic_auto_plan(self) -> None:
        def planner(_prompt: str) -> str:
            return json.dumps({"nodes": [{"node_id": "bad", "capability_id": "sql_query.sql_generate"}]})

        plan = await self.make_provider(planner).build_plan(
            OrchestrationRequest(
                task_id="task-3",
                conversation_id="conv-1",
                root_message_id="msg-1",
                user_message="查询龙粳33",
            )
        )

        self.assertEqual(plan.metadata["route"], "auto")
        self.assertTrue(plan.metadata["planner_fallback_used"])
        self.assertEqual(plan.metadata["planner_fallback_reason"], "WorkflowPlanValidationError")
        self.assertIn("sql_query.intent_route", [node.capability_id for node in plan.nodes])

    async def test_invalid_planner_json_falls_back(self) -> None:
        plan = await self.make_provider(lambda _prompt: "not json").build_plan(
            OrchestrationRequest(
                task_id="task-4",
                conversation_id="conv-1",
                root_message_id="msg-1",
                user_message="你好",
            )
        )

        self.assertEqual([node.capability_id for node in plan.nodes], ["main_agent.respond"])
        self.assertTrue(plan.metadata["planner_fallback_used"])
        self.assertEqual(plan.metadata["planner_fallback_reason"], "PlannerOutputError")


if __name__ == "__main__":
    unittest.main()
