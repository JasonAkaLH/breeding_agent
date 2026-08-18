from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from src.capabilities.main_agent import (
    MAIN_AGENT_CAPABILITY_DESCRIPTORS,
    MAIN_AGENT_PLANNER_PAYLOAD_POLICIES,
    MainAgentWorkflowProvider,
)
from src.orchestration.auto_workflow_provider import AutoWorkflowProvider
from src.orchestration.llm_workflow_provider import LLMWorkflowProvider, WorkflowPlanningError
from src.orchestration.skill_workflow_provider import SkillWorkflowProvider
from src.integrations.agent_skills import SkillManifest
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
        self.generic_data_lookup_manifest = SkillManifest(
            name="generic-data-lookup",
            description="安全回答品种、审定、基因型、表型和数据库类只读查询问题。",
            triggers=("查询品种", "审定信息", "基因型", "数据库查询"),
            body="# Data Query",
            source_path=Path("skill/generic-data-lookup/SKILL.md"),
            metadata={"execution": {"mode": "platform_service", "handler": "skill.data_lookup.platform_handler", "answer_mode": "requires_finalizer"}},
        )
        self.registry.register(
            CapabilityDescriptor(
                capability_id="skill.generic_data_lookup",
                name="generic-data-lookup",
                description=self.generic_data_lookup_manifest.description,
                kind="skill",
                source="skill",
                source_path="generic-data-lookup/SKILL.md",
            ),
            planner_payload_policy=CapabilityPayloadPolicy(
                planner_allowed_fields=("subtask_label", "parent_question"),
                system_payload_factory=lambda request: {"user_message": request.effective_user_message},
            ),
        )
        self.skill_provider = SkillWorkflowProvider(
            {"skill.generic_data_lookup": "generic-data-lookup"},
            skill_manifest_resolver=lambda capability_id, _revision: self.generic_data_lookup_manifest if capability_id == "skill.generic_data_lookup" else None,
        )
        self.fallback_provider = AutoWorkflowProvider(main_agent_provider=MainAgentWorkflowProvider())

    def make_provider(self, text_generator):
        return LLMWorkflowProvider(
            capability_registry=self.registry,
            fallback_provider=self.fallback_provider,
            macro_providers={"skill.generic_data_lookup": self.skill_provider},
            text_generator=text_generator,
        )

    def make_provider_with_payload_policies(self, text_generator, payload_policies):
        return LLMWorkflowProvider(
            capability_registry=self.registry,
            fallback_provider=self.fallback_provider,
            macro_providers={"skill.generic_data_lookup": self.skill_provider},
            text_generator=text_generator,
            payload_policies=payload_policies,
        )

    async def test_model_node_ids_are_canonicalized_per_task_after_finalizer_enrichment(self) -> None:
        self.registry.register(
            CapabilityDescriptor(
                capability_id="field.inspect",
                name="Field Inspect",
                description="Inspect a field without producing the final answer.",
                public=True,
            )
        )

        async def planner(_prompt: str) -> str:
            return json.dumps(
                {
                    "nodes": [
                        {
                            "node_id": "n1",
                            "capability_id": "field.inspect",
                        }
                    ]
                }
            )

        first = await self.make_provider(planner).build_plan(
            OrchestrationRequest(
                task_id="task-first",
                conversation_id="conv-1",
                root_message_id="msg-1",
                user_message="查询龙粳33",
            )
        )
        second = await self.make_provider(planner).build_plan(
            OrchestrationRequest(
                task_id="task-second",
                conversation_id="conv-2",
                root_message_id="msg-2",
                user_message="查询龙粳33",
            )
        )

        first_public = tuple(node for node in first.nodes if node.metadata.get("identity_origin") == "model")
        second_public = tuple(node for node in second.nodes if node.metadata.get("identity_origin") == "model")
        self.assertEqual(len(first_public), 2)
        self.assertEqual(len(second_public), 2)
        self.assertTrue(all(node.node_id.startswith("task-first:plan:v1:p0:") for node in first_public))
        self.assertTrue(all(node.node_id.startswith("task-second:plan:v1:p0:") for node in second_public))
        self.assertEqual(first_public[1].depends_on, (first_public[0].node_id,))
        self.assertTrue(set(node.node_id for node in first_public).isdisjoint(node.node_id for node in second_public))

    async def test_planner_explicit_capability_missing_metadata_becomes_disclosed_fallback(self) -> None:
        async def planner(_prompt: str) -> str:
            return json.dumps(
                {
                    "nodes": [
                        {
                            "node_id": "answer",
                            "capability_id": "main_agent.respond",
                            "metadata": {
                                "capability_missing_fallback": {
                                    "enabled": True,
                                    "scope": "full",
                                    "reason_code": "capability_missing",
                                    "missing_capability_summary": "缺少田间图生成能力",
                                    "fallback_content_scope": "只能给出手工建议",
                                    "llm_fallback_allowed": True,
                                    "artifact_generation_allowed": False,
                                    "disclosure_required": True,
                                    "memory_context_used": False,
                                    "source_message_count": 1,
                                }
                            },
                        }
                    ]
                }
            )

        provider = LLMWorkflowProvider(
            capability_registry=self.registry,
            fallback_provider=self.fallback_provider,
            macro_providers={"skill.generic_data_lookup": self.skill_provider},
            text_generator=planner,
            max_repair_attempts=0,
        )

        plan = await provider.build_plan(
            OrchestrationRequest(
                task_id="task-missing",
                conversation_id="conv-1",
                root_message_id="msg-1",
                user_message="请生成田间图文件",
            )
        )

        self.assertEqual([node.capability_id for node in plan.nodes], ["main_agent.respond"])
        fallback_metadata = plan.nodes[0].metadata["capability_missing_fallback"]
        self.assertEqual(fallback_metadata["reason_code"], "capability_missing")
        self.assertFalse(fallback_metadata["artifact_generation_allowed"])
        self.assertFalse(plan.nodes[0].metadata["auto_skill_matching_enabled"])

    async def test_planner_top_level_capability_missing_metadata_is_applied_to_main_node(self) -> None:
        async def planner(_prompt: str) -> str:
            return json.dumps(
                {
                    "metadata": {
                        "capability_missing_fallback": {
                            "enabled": True,
                            "scope": "full",
                            "reason_code": "capability_missing",
                            "missing_capability_summary": "缺少田间图生成能力",
                            "fallback_content_scope": "只能给出手工建议",
                            "llm_fallback_allowed": True,
                            "artifact_generation_allowed": False,
                            "disclosure_required": True,
                            "memory_context_used": False,
                            "source_message_count": 1,
                        }
                    },
                    "nodes": [{"node_id": "answer", "capability_id": "main_agent.respond"}],
                }
            )

        plan = await self.make_provider(planner).build_plan(
            OrchestrationRequest(
                task_id="task-top-fallback",
                conversation_id="conv-1",
                root_message_id="msg-1",
                user_message="请生成田间图文件",
            )
        )

        self.assertEqual(plan.nodes[0].metadata["capability_missing_fallback"]["reason_code"], "capability_missing")
        self.assertFalse(plan.nodes[0].metadata["auto_skill_matching_enabled"])

    async def test_top_level_capability_missing_metadata_is_applied_to_synthesized_finalizer(self) -> None:
        self.registry.register(
            CapabilityDescriptor("field_map.generate", "Field Map", "Generate field map.", public=True)
        )

        async def planner(_prompt: str) -> str:
            return json.dumps(
                {
                    "metadata": {
                        "capability_missing_fallback": {
                            "enabled": True,
                            "scope": "partial",
                            "reason_code": "capability_missing",
                            "missing_capability_summary": "缺少绘图能力",
                            "attempted_capability_summary": "已查询基础数据",
                            "fallback_content_scope": "只能给出手工绘图建议",
                            "llm_fallback_allowed": True,
                            "artifact_generation_allowed": False,
                            "disclosure_required": True,
                            "memory_context_used": False,
                            "source_message_count": 1,
                        }
                    },
                    "nodes": [{"node_id": "draw", "capability_id": "field_map.generate"}],
                }
            )

        plan = await self.make_provider(planner).build_plan(
            OrchestrationRequest(
                task_id="task-top-finalizer-fallback",
                conversation_id="conv-1",
                root_message_id="msg-1",
                user_message="查询龙粳33后生成田间图",
            )
        )

        finalizer = plan.nodes[-1]
        self.assertEqual(finalizer.capability_id, "main_agent.respond")
        self.assertTrue(plan.metadata["planner_finalizer_added"])
        self.assertEqual(finalizer.metadata["capability_missing_fallback"]["scope"], "partial")
        self.assertFalse(finalizer.metadata["auto_skill_matching_enabled"])

    async def test_unknown_planner_capability_fails_instead_of_deterministic_fallback(self) -> None:
        async def planner(_prompt: str) -> str:
            return json.dumps({"nodes": [{"node_id": "draw", "capability_id": "skill.deleted_drawer"}]})

        provider = LLMWorkflowProvider(
            capability_registry=self.registry,
            fallback_provider=self.fallback_provider,
            macro_providers={"skill.generic_data_lookup": self.skill_provider},
            text_generator=planner,
            max_repair_attempts=0,
        )

        with self.assertRaises(WorkflowPlanningError) as raised:
            await provider.build_plan(
                OrchestrationRequest(
                    task_id="task-missing",
                    conversation_id="conv-1",
                    root_message_id="msg-1",
                    user_message="请生成田间图文件",
                )
            )

        self.assertEqual(raised.exception.reason, "WorkflowPlanValidationError")

    async def test_llm_generic_data_lookup_plan_is_validated_expanded_and_enriched(self) -> None:
        prompts: list[str] = []

        async def planner(prompt: str) -> str:
            prompts.append(prompt)
            return json.dumps(
                {
                    "nodes": [
                        {"node_id": "query_data", "capability_id": "skill.generic_data_lookup"},
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
        self.assertEqual(capability_ids, ["skill.generic_data_lookup", "main_agent.respond"])
        self.assertEqual(plan.nodes[-1].depends_on, (plan.nodes[0].node_id,))
        self.assertIn(":plan:v1:p0:query_data:", plan.nodes[0].node_id)
        self.assertEqual(plan.nodes[-1].input_payload["user_message"], "查询龙粳33的详细审定信息")
        self.assertIn("skill.generic_data_lookup", prompts[0])
        self.assertIn("main_agent.respond", prompts[0])
        self.assertNotIn("internal.generate", prompts[0])

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

    async def test_generic_data_lookup_only_plan_gets_main_agent_finalizer(self) -> None:
        def planner(_prompt: str) -> str:
            return json.dumps({"nodes": [{"node_id": "query_data", "capability_id": "skill.generic_data_lookup"}]})

        plan = await self.make_provider(planner).build_plan(
            OrchestrationRequest(
                task_id="task-finalizer",
                conversation_id="conv-1",
                root_message_id="msg-1",
                user_message="查询龙粳33",
            )
        )

        self.assertEqual(plan.nodes[-1].capability_id, "main_agent.respond")
        self.assertEqual(plan.nodes[-1].depends_on, (plan.nodes[0].node_id,))
        self.assertIn(":plan:v1:p0:query_data:", plan.nodes[0].node_id)
        self.assertFalse(plan.metadata["planner_finalizer_added"])

    async def test_planner_payload_cannot_override_user_input(self) -> None:
        def planner(_prompt: str) -> str:
            return json.dumps(
                {
                    "nodes": [
                        {
                            "node_id": "query_data",
                            "capability_id": "skill.generic_data_lookup",
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

        self.assertEqual(plan.nodes[0].input_payload["user_message"], "查询龙粳33")
        self.assertEqual(plan.nodes[-1].input_payload["user_message"], "查询龙粳33")
        self.assertNotIn("恶意替换", str(plan.nodes[0].input_payload))
        self.assertNotIn("恶意替换", str(plan.nodes[-1].input_payload))

    async def test_generic_data_lookup_payload_policy_drops_route_hint_but_keeps_subtask_context(self) -> None:
        def planner(_prompt: str) -> str:
            return json.dumps(
                {
                    "nodes": [
                        {
                            "node_id": "query_genotype_info",
                            "capability_id": "skill.generic_data_lookup",
                            "input_payload": {
                                "user_question": "恶意替换查询",
                                "route_hint": "dataset_b",
                                "subtask_label": "基因型信息",
                                "parent_question": "龙粳33的审定信息和基因型信息都查一下",
                                "allowed_tables": ["should_not_pass"],
                            },
                        }
                    ]
                }
            )

        plan = await self.make_provider(planner).build_plan(
            OrchestrationRequest(
                task_id="task-sql-hints",
                conversation_id="conv-1",
                root_message_id="msg-1",
                user_message="龙粳33的审定信息和基因型信息都查一下",
            )
        )

        skill_node = next(node for node in plan.nodes if node.capability_id == "skill.generic_data_lookup")
        self.assertIn(":plan:v1:p0:query_genotype_info:", skill_node.node_id)
        self.assertEqual(skill_node.input_payload["user_message"], "龙粳33的审定信息和基因型信息都查一下")
        self.assertNotIn("route_hint", skill_node.input_payload)
        self.assertEqual(skill_node.input_payload["subtask_label"], "基因型信息")
        self.assertEqual(skill_node.input_payload["parent_question"], "龙粳33的审定信息和基因型信息都查一下")
        self.assertNotIn("allowed_tables", skill_node.input_payload)

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
                                "username": "planner-account",
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
        self.assertNotIn("username", plan.nodes[0].input_payload)
        self.assertEqual(plan.nodes[-1].capability_id, "main_agent.respond")
        self.assertIn("report.generate", prompts[0])
        self.assertIn("规划器 input_payload 允许字段：format, max_sections。", prompts[0])

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
                                "username": "planner-account",
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

    async def test_generic_data_lookup_and_unwired_main_agent_plan_is_rewired_to_use_query_result(self) -> None:
        def planner(_prompt: str) -> str:
            return json.dumps(
                {
                    "nodes": [
                        {"node_id": "query_data", "capability_id": "skill.generic_data_lookup"},
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
        self.assertEqual(plan.nodes[-1].depends_on, (plan.nodes[0].node_id,))
        self.assertIn(":plan:v1:p0:query_data:", plan.nodes[0].node_id)
        self.assertFalse(plan.metadata["planner_finalizer_rewired"])
        self.assertFalse(plan.metadata["planner_finalizer_added"])

    async def test_planner_provider_exception_fails_without_deterministic_fallback(self) -> None:
        def planner(_prompt: str) -> str:
            raise RuntimeError("planner unavailable")

        with self.assertRaises(WorkflowPlanningError) as raised:
            await self.make_provider(planner).build_plan(
                OrchestrationRequest(
                    task_id="task-provider-fail",
                    conversation_id="conv-1",
                    root_message_id="msg-1",
                    user_message="你好",
                )
            )

        self.assertEqual(raised.exception.reason, "RuntimeError")
        self.assertEqual(raised.exception.attempts, 1)

    async def test_internal_capability_output_is_repaired_by_llm_instead_of_deterministic_fallback(self) -> None:
        prompts: list[str] = []

        def planner(prompt: str) -> str:
            prompts.append(prompt)
            if len(prompts) == 1:
                return json.dumps({"nodes": [{"node_id": "bad", "capability_id": "internal.generate"}]})
            return json.dumps({"nodes": [{"node_id": "query_data", "capability_id": "skill.generic_data_lookup"}]})

        plan = await self.make_provider(planner).build_plan(
            OrchestrationRequest(
                task_id="task-3",
                conversation_id="conv-1",
                root_message_id="msg-1",
                user_message="查询龙粳33",
            )
        )

        self.assertEqual(plan.metadata["route"], "llm_planner")
        self.assertFalse(plan.metadata["planner_fallback_used"])
        self.assertEqual(plan.metadata["planner_repair_attempts"], 1)
        self.assertIn("上一轮 Planner 输出未通过校验", prompts[1])
        self.assertIn("skill.generic_data_lookup", [node.capability_id for node in plan.nodes])

    async def test_planner_repair_profile_records_profile_metadata_and_limits_previous_output(self) -> None:
        calls: list[dict] = []
        noisy_output = "not json " + ("RAW_OUTPUT_SHOULD_BE_TRUNCATED" * 200)

        def planner(prompt: str, **kwargs) -> str:
            calls.append({"prompt": prompt, "prompt_profile": kwargs.get("prompt_profile")})
            if len(calls) == 1:
                return noisy_output
            return json.dumps({"nodes": [{"node_id": "query_data", "capability_id": "skill.generic_data_lookup"}]})

        with patch.dict("os.environ", {"MAF_PROMPT_ENVELOPE_MODE": "string"}):
            plan = await self.make_provider(planner).build_plan(
                OrchestrationRequest(
                    task_id="task-repair-profile",
                    conversation_id="conv-1",
                    root_message_id="msg-1",
                    user_message="查询龙粳33",
                    metadata={"trim_max_tokens": 12_000},
                )
            )

        self.assertEqual(plan.metadata["planner_repair_attempts"], 1)
        self.assertEqual(calls[0]["prompt_profile"]["template_id"], "planner")
        self.assertEqual(calls[1]["prompt_profile"]["template_id"], "planner_repair")
        self.assertIn("上一轮原始输出", calls[1]["prompt"])
        self.assertIn(noisy_output[:2000], calls[1]["prompt"])
        self.assertNotIn(noisy_output[2100:], calls[1]["prompt"])
        self.assertLessEqual(
            calls[1]["prompt_profile"]["final_input_tokens"],
            calls[1]["prompt_profile"]["final_input_token_budget"],
        )

    async def test_invalid_planner_json_is_repaired_by_llm(self) -> None:
        prompts: list[str] = []

        def planner(prompt: str) -> str:
            prompts.append(prompt)
            if len(prompts) == 1:
                return "not json"
            return json.dumps({"nodes": [{"node_id": "answer_user", "capability_id": "main_agent.respond"}]})

        plan = await self.make_provider(planner).build_plan(
            OrchestrationRequest(
                task_id="task-4",
                conversation_id="conv-1",
                root_message_id="msg-1",
                user_message="你好",
            )
        )

        self.assertEqual([node.capability_id for node in plan.nodes], ["main_agent.respond"])
        self.assertEqual(plan.metadata["route"], "llm_planner")
        self.assertFalse(plan.metadata["planner_fallback_used"])
        self.assertEqual(plan.metadata["planner_repair_attempts"], 1)
        self.assertIn("not json", prompts[1])

    async def test_invalid_planner_json_fails_after_llm_repair_attempt_is_exhausted(self) -> None:
        prompts: list[str] = []

        def planner(prompt: str) -> str:
            prompts.append(prompt)
            return "still not json"

        with self.assertRaises(WorkflowPlanningError) as raised:
            await self.make_provider(planner).build_plan(
                OrchestrationRequest(
                    task_id="task-invalid-after-repair",
                    conversation_id="conv-1",
                    root_message_id="msg-1",
                    user_message="你好",
                )
            )

        self.assertEqual(len(prompts), 2)
        self.assertEqual(raised.exception.reason, "PlannerOutputError")
        self.assertEqual(raised.exception.attempts, 2)

    async def test_skill_public_capability_is_visible_and_expands_to_forced_main_agent_without_extra_finalizer(self) -> None:
        self.registry.register(
            CapabilityDescriptor(
                capability_id="skill.mini_breedstat_rcbd",
                name="mini-breedstat-rcbd",
                description="生成 RCBD 随机区组设计",
                kind="skill",
                source="skill",
            )
        )
        prompts: list[str] = []

        def planner(prompt: str) -> str:
            prompts.append(prompt)
            return json.dumps({
                "nodes": [
                    {
                        "node_id": "design_rcbd",
                        "capability_id": "skill.mini_breedstat_rcbd",
                        "input_payload": {"forced_skill_name": "evil", "script_path": "hack.py"},
                    }
                ]
            })

        provider = LLMWorkflowProvider(
            capability_registry=self.registry,
            fallback_provider=self.fallback_provider,
            macro_providers={
                "skill.generic_data_lookup": self.skill_provider,
                "skill.mini_breedstat_rcbd": SkillWorkflowProvider({"skill.mini_breedstat_rcbd": "mini-breedstat-rcbd"}),
            },
            text_generator=planner,
        )
        plan = await provider.build_plan(
            OrchestrationRequest(
                task_id="task-skill",
                conversation_id="conv-1",
                root_message_id="msg-1",
                user_message="用上传材料做随机区组设计",
            )
        )

        self.assertIn("skill.mini_breedstat_rcbd", prompts[0])
        self.assertEqual([node.capability_id for node in plan.nodes], ["main_agent.respond"])
        self.assertEqual(plan.nodes[0].input_payload, {"user_message": "用上传材料做随机区组设计"})
        self.assertEqual(plan.nodes[0].metadata["forced_skill_capability_id"], "skill.mini_breedstat_rcbd")
        self.assertEqual(plan.nodes[0].metadata["forced_skill_name"], "mini-breedstat-rcbd")
        self.assertEqual(plan.nodes[0].metadata["forced_skill_source"], "planner")
        self.assertFalse(plan.metadata["planner_finalizer_added"])


if __name__ == "__main__":
    unittest.main()
