from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path

from src.capabilities.main_agent.runtime_replanner import MainAgentRuntimeReplanner
from src.capabilities.main_agent.workflow import MAIN_AGENT_CAPABILITY_DESCRIPTORS, MAIN_AGENT_PLANNER_PAYLOAD_POLICIES
from src.core.enums import NodeStatus
from src.core.models import TaskNode
from src.integrations.codex_skills import SkillManifest
from src.orchestration.completion_policy import CompletionStatus
from src.orchestration.models import CapabilityDescriptor, OrchestrationRequest, WorkflowNodePlan, WorkflowPlan
from src.orchestration.planner_payload_policy import CapabilityPayloadPolicy
from src.orchestration.registry import CapabilityRegistry
from src.orchestration.runtime_replanner import RuntimeReplanContext
from src.orchestration.skill_workflow_provider import SkillWorkflowProvider


class MainAgentRuntimeReplannerTest(unittest.TestCase):
    def _generic_data_lookup_manifest(self) -> SkillManifest:
        return SkillManifest(
            name="generic-data-lookup",
            description="通过项目级 Skill platform-service 安全回答数据库类只读查询问题。",
            triggers=("查询品种", "审定信息", "基因型", "数据库查询"),
            body="# DataLookup Skill",
            source_path=Path("skill/generic-data-lookup/SKILL.md"),
            metadata={
                "execution": {
                    "mode": "platform_service",
                    "handler": "skill.data_lookup.platform_handler",
                    "answer_mode": "requires_finalizer",
                }
            },
        )

    def _registry(self) -> CapabilityRegistry:
        registry = CapabilityRegistry()
        for descriptor in MAIN_AGENT_CAPABILITY_DESCRIPTORS:
            registry.register(
                descriptor,
                planner_payload_policy=MAIN_AGENT_PLANNER_PAYLOAD_POLICIES.get(descriptor.capability_id),
            )
        registry.register(
            CapabilityDescriptor(
                capability_id="skill.generic_data_lookup",
                name="generic-data-lookup",
                description="通过项目级 Skill platform-service 安全回答数据库类只读查询问题。",
                kind="skill",
                source="skill",
                source_path="generic-data-lookup/SKILL.md",
            ),
            planner_payload_policy=CapabilityPayloadPolicy(
                planner_allowed_fields=("subtask_label", "parent_question"),
                system_payload_factory=lambda request: {"user_message": request.effective_user_message},
            ),
        )
        return registry

    def _macro_providers(self) -> dict[str, SkillWorkflowProvider]:
        manifest = self._generic_data_lookup_manifest()
        return {
            "skill.generic_data_lookup": SkillWorkflowProvider(
                {"skill.generic_data_lookup": "generic-data-lookup"},
                skill_manifest_resolver=lambda capability_id, _revision: manifest if capability_id == "skill.generic_data_lookup" else None,
            )
        }

    def test_llm_runtime_replanner_returns_expanded_revised_skill_plan_from_unsatisfied_output(self) -> None:
        calls: list[dict] = []

        async def text_generator(prompt: str, *, request=None, stage: str | None = None) -> str:
            calls.append({"prompt": prompt, "request": request, "stage": stage})
            return json.dumps(
                {
                    "action": "replan",
                    "reason": "split query after empty result",
                    "nodes": [
                        {"node_id": "query_again", "capability_id": "skill.generic_data_lookup"},
                        {"node_id": "answer_user", "capability_id": "main_agent.respond", "depends_on": ["query_again"]},
                    ],
                },
                ensure_ascii=False,
            )

        request = OrchestrationRequest(
            task_id="task-1",
            conversation_id="conv-1",
            root_message_id="msg-1",
            user_message="查询龙粳33",
        )
        context = RuntimeReplanContext(
            request=request,
            plan=WorkflowPlan(
                task_id="task-1",
                nodes=(WorkflowNodePlan(node_id="query_data", capability_id="skill.generic_data_lookup"),),
                max_replans=1,
                max_dynamic_nodes=24,
            ),
            nodes={
                "filter": TaskNode(
                    node_id="filter",
                    task_id="task-1",
                    capability_id="skill.generic_data_lookup",
                    status=NodeStatus.COMPLETED,
                )
            },
            node_outputs={
                "filter": {
                    "row_count": 0,
                    "satisfaction": {"satisfied": False, "reason_code": "empty_result", "replan_recommended": True},
                }
            },
            completion_status=CompletionStatus.RUNNING,
        )
        replanner = MainAgentRuntimeReplanner(
            capability_registry=self._registry(),
            macro_providers=self._macro_providers(),
            text_generator=text_generator,
        )

        decision = asyncio.run(replanner.build_replan(context))

        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertEqual(decision.reason, "split query after empty result")
        self.assertEqual(decision.metadata["replan_source"], "main_agent_llm_runtime")
        self.assertEqual(calls[0]["stage"], "orchestration_replan")
        capabilities = [node.capability_id for node in decision.plan.nodes]
        self.assertEqual(capabilities, ["skill.generic_data_lookup", "main_agent.respond"])
        self.assertEqual(decision.plan.nodes[-1].capability_id, "main_agent.respond")
        self.assertEqual(decision.plan.metadata["runtime_replan_source"], "main_agent_llm_runtime")

    def test_runtime_replanner_skill_capability_expands_with_replanner_source(self) -> None:
        async def text_generator(_prompt: str, **_: object) -> str:
            return json.dumps(
                {
                    "action": "replan",
                    "reason": "use rcbd skill",
                    "nodes": [{"node_id": "design", "capability_id": "skill.mini_breedstat_rcbd"}],
                },
                ensure_ascii=False,
            )

        registry = self._registry()
        registry.register(
            CapabilityDescriptor(
                capability_id="skill.mini_breedstat_rcbd",
                name="mini-breedstat-rcbd",
                description="生成 RCBD 随机区组设计",
                kind="skill",
                source="skill",
            )
        )
        request = OrchestrationRequest(
            task_id="task-skill-replan",
            conversation_id="conv-1",
            root_message_id="msg-1",
            user_message="做随机区组设计",
        )
        context = RuntimeReplanContext(
            request=request,
            plan=WorkflowPlan(
                task_id="task-skill-replan",
                nodes=(WorkflowNodePlan(node_id="answer", capability_id="main_agent.respond"),),
                max_replans=1,
                max_dynamic_nodes=4,
            ),
            nodes={
                "answer": TaskNode(
                    node_id="answer",
                    task_id="task-skill-replan",
                    capability_id="main_agent.respond",
                    status=NodeStatus.COMPLETED,
                )
            },
            node_outputs={
                "answer": {
                    "satisfaction": {
                        "satisfied": False,
                        "reason_code": "needs_skill",
                        "replan_recommended": True,
                    }
                }
            },
            completion_status=CompletionStatus.RUNNING,
        )
        replanner = MainAgentRuntimeReplanner(
            capability_registry=registry,
            macro_providers={
                **self._macro_providers(),
                "skill.mini_breedstat_rcbd": SkillWorkflowProvider(
                    {"skill.mini_breedstat_rcbd": "mini-breedstat-rcbd"}
                ),
            },
            text_generator=text_generator,
        )

        decision = asyncio.run(replanner.build_replan(context))

        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertEqual([node.capability_id for node in decision.plan.nodes], ["main_agent.respond"])
        self.assertEqual(decision.plan.nodes[0].metadata["forced_skill_source"], "replanner")
        self.assertEqual(decision.plan.nodes[0].metadata["forced_skill_name"], "mini-breedstat-rcbd")

    def test_does_not_call_llm_when_outputs_are_satisfied(self) -> None:
        calls: list[str] = []

        async def text_generator(prompt: str, **_: object) -> str:
            calls.append(prompt)
            return json.dumps({"action": "none"})

        request = OrchestrationRequest(task_id="task-2", conversation_id="conv-1", root_message_id="msg-1", user_message="你好")
        context = RuntimeReplanContext(
            request=request,
            plan=WorkflowPlan(task_id="task-2", nodes=(), max_replans=1, max_dynamic_nodes=1),
            nodes={},
            node_outputs={"answer": {"satisfaction": {"satisfied": True, "replan_recommended": False}}},
            completion_status=CompletionStatus.COMPLETED,
        )
        replanner = MainAgentRuntimeReplanner(
            capability_registry=self._registry(),
            macro_providers=self._macro_providers(),
            text_generator=text_generator,
        )

        decision = asyncio.run(replanner.build_replan(context))

        self.assertIsNone(decision)
        self.assertEqual(calls, [])

    def test_does_not_consume_soft_skill_execute_signal(self) -> None:
        calls: list[str] = []

        async def text_generator(prompt: str, **_: object) -> str:
            calls.append(prompt)
            return json.dumps({"action": "replan", "nodes": [{"node_id": "bad", "capability_id": "skill.generic_data_lookup"}]})

        context = RuntimeReplanContext(
            request=OrchestrationRequest(
                task_id="task-soft",
                conversation_id="conv-1",
                root_message_id="msg-1",
                user_message="执行",
                metadata={"soft_skill_binding": {"capability_id": "skill.generic_data_lookup"}},
            ),
            plan=WorkflowPlan(
                task_id="task-soft",
                nodes=(WorkflowNodePlan(node_id="answer", capability_id="main_agent.respond"),),
                max_replans=1,
                max_dynamic_nodes=4,
            ),
            nodes={"answer": TaskNode("answer", "task-soft", "main_agent.respond", status=NodeStatus.COMPLETED)},
            node_outputs={
                "answer": {
                    "soft_skill_decision": {"decision": "execute", "target_capability_id": "skill.generic_data_lookup"},
                    "satisfaction": {"satisfied": False, "replan_recommended": True, "reason_code": "soft_skill_execute"},
                }
            },
            completion_status=CompletionStatus.RUNNING,
        )
        replanner = MainAgentRuntimeReplanner(
            capability_registry=self._registry(),
            macro_providers=self._macro_providers(),
            text_generator=text_generator,
        )

        self.assertIsNone(asyncio.run(replanner.build_replan(context)))
        self.assertEqual(calls, [])

    def test_replan_prompt_uses_sanitized_observation_without_sensitive_outputs(self) -> None:
        prompts: list[str] = []

        async def text_generator(prompt: str, **_: object) -> str:
            prompts.append(prompt)
            return json.dumps({"action": "none"})

        request = OrchestrationRequest(task_id="task-sensitive", conversation_id="conv-1", root_message_id="msg-1", user_message="查询龙粳33")
        context = RuntimeReplanContext(
            request=request,
            plan=WorkflowPlan(task_id="task-sensitive", nodes=(), max_replans=1, max_dynamic_nodes=1),
            nodes={
                "filter": TaskNode(
                    node_id="filter",
                    task_id="task-sensitive",
                    capability_id="skill.generic_data_lookup",
                    status=NodeStatus.COMPLETED,
                )
            },
            node_outputs={
                "filter": {
                    "sql": "SELECT * FROM secret_table",
                    "guard_pass_token": "SECRET_TOKEN_SHOULD_NOT_LEAK",
                    "schema_ddl": "CREATE TABLE secret_table(secret text)",
                    "rows": [
                        {"variety_name": "龙粳33", "very_long_detail": "x" * 500},
                        {"variety_name": "龙粳34", "very_long_detail": "y" * 500},
                        {"variety_name": "龙粳35", "very_long_detail": "z" * 500},
                    ],
                    "row_count": 3,
                    "route_id": "dataset_b",
                    "satisfaction": {"satisfied": False, "reason_code": "no_relevant_rows_after_filtering", "replan_recommended": True},
                }
            },
            completion_status=CompletionStatus.RUNNING,
        )
        replanner = MainAgentRuntimeReplanner(
            capability_registry=self._registry(),
            macro_providers=self._macro_providers(),
            text_generator=text_generator,
        )

        decision = asyncio.run(replanner.build_replan(context))

        self.assertIsNone(decision)
        self.assertEqual(len(prompts), 1)
        prompt = prompts[0]
        self.assertIn("row_sample", prompt)
        self.assertIn("龙粳33", prompt)
        self.assertNotIn("SECRET_TOKEN_SHOULD_NOT_LEAK", prompt)
        self.assertNotIn("SELECT * FROM secret_table", prompt)
        self.assertNotIn("CREATE TABLE secret_table", prompt)
        self.assertNotIn("龙粳35", prompt)
        self.assertNotIn("x" * 500, prompt)


if __name__ == "__main__":
    unittest.main()
