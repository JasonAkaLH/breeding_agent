from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.integrations.agent_skills.skill_runtime_state import SkillRuntimeState
from src.orchestration.answer_roles import RESPONSE_ROLE_FINAL
from src.orchestration.models import OrchestrationRequest, WorkflowNodePlan, WorkflowPlan
from src.orchestration.skill_workflow_provider import SkillWorkflowProvider
from src.orchestration.workflow_expander import WorkflowExpander


class WorkflowExpanderTest(unittest.TestCase):
    def _generic_data_lookup_skill_provider(self, root: Path) -> tuple[SkillWorkflowProvider, str]:
        return self._platform_skill_provider(root, (("generic-data-lookup", "skill.generic_data_lookup"),))

    def _platform_skill_provider(self, root: Path, skills: tuple[tuple[str, str], ...]) -> tuple[SkillWorkflowProvider, str]:
        for skill_name, capability_id in skills:
            self._write_platform_skill(root, skill_name=skill_name, capability_id=capability_id)
        state = SkillRuntimeState.from_roots(
            skill_roots=(root,),
            public_skill_roots=(root,),
            reserved_capability_ids=("main_agent.respond",),
        )
        provider = SkillWorkflowProvider(
            skill_name_resolver=lambda capability_id, revision: state.skill_name_for_capability(capability_id, revision),
            skill_manifest_resolver=lambda capability_id, revision: state.catalog_for_revision(revision).get(state.skill_name_for_capability(capability_id, revision) or ""),
        )
        return provider, state.active_revision

    @staticmethod
    def _write_platform_skill(root: Path, *, skill_name: str, capability_id: str) -> None:
        skill_dir = root / skill_name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            f"""---
name: {skill_name}
capability_id: {capability_id}
description: 通过平台服务执行 DataLookup
execution:
  mode: platform_service
  handler: skill.data_lookup.platform_handler
  answer_mode: requires_finalizer
---

# DataLookup Skill
""",
            encoding="utf-8",
        )

    def test_expands_generic_data_lookup_skill_platform_service_and_rewires_downstream_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "skill"
            skill_provider, revision = self._generic_data_lookup_skill_provider(root)
            request = OrchestrationRequest(
                task_id="task-1",
                conversation_id="conv-1",
                root_message_id="msg-1",
                user_message="查询龙粳33的基因型信息",
                metadata={"skill_bundle_revision": revision},
            )
            high_level = WorkflowPlan(
                task_id="task-1",
                nodes=(
                    WorkflowNodePlan(node_id="query_data", capability_id="skill.generic_data_lookup"),
                    WorkflowNodePlan(node_id="answer_user", capability_id="main_agent.respond", depends_on=("query_data",)),
                ),
            )

            expanded = WorkflowExpander(
                {},
                macro_provider_resolver=lambda capability_id: skill_provider if capability_id.startswith("skill.") else None,
            ).expand(high_level, request=request)

        self.assertEqual(expanded.task_id, "task-1")
        self.assertEqual([node.capability_id for node in expanded.nodes], ["skill.generic_data_lookup", "main_agent.respond"])
        self.assertEqual(expanded.nodes[0].node_id, "task-1:query_data:skill_execute")
        self.assertEqual(expanded.nodes[1].depends_on, ("task-1:query_data:skill_execute",))
        self.assertEqual(expanded.nodes[1].metadata["response_role"], RESPONSE_ROLE_FINAL)
        self.assertEqual(expanded.nodes[1].metadata["answer_scope"], "task")
        self.assertEqual(expanded.metadata["expanded_macro_nodes"]["query_data"]["capability_id"], "skill.generic_data_lookup")

    def test_macro_roots_depend_on_high_level_dependencies_for_skill_executor_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "skill"
            skill_provider, revision = self._generic_data_lookup_skill_provider(root)
            request = OrchestrationRequest(
                task_id="task-2",
                conversation_id="conv-1",
                root_message_id="msg-1",
                user_message="查询龙粳33",
                metadata={"skill_bundle_revision": revision},
            )
            high_level = WorkflowPlan(
                task_id="task-2",
                nodes=(
                    WorkflowNodePlan(node_id="prepare_context", capability_id="main_agent.respond"),
                    WorkflowNodePlan(node_id="query_data", capability_id="skill.generic_data_lookup", depends_on=("prepare_context",)),
                ),
            )

            expanded = WorkflowExpander(
                {},
                macro_provider_resolver=lambda capability_id: skill_provider if capability_id.startswith("skill.") else None,
            ).expand(high_level, request=request)

        skill_node = next(node for node in expanded.nodes if node.capability_id == "skill.generic_data_lookup")
        self.assertEqual(skill_node.depends_on, ("prepare_context",))

    def test_dynamic_macro_provider_resolver_expands_skill_capability_with_revision(self) -> None:
        skill_provider = SkillWorkflowProvider(
            skill_name_resolver=lambda capability_id, revision: (
                "demo-hot-reload" if capability_id == "skill.demo_hot_reload" and revision == "skillrev-1" else None
            )
        )
        request = OrchestrationRequest(
            task_id="task-skill-dynamic",
            conversation_id="conv-1",
            root_message_id="msg-1",
            user_message="请处理动态加载任务",
            metadata={"skill_bundle_revision": "skillrev-1"},
        )
        high_level = WorkflowPlan(
            task_id="task-skill-dynamic",
            nodes=(WorkflowNodePlan(node_id="demo", capability_id="skill.demo_hot_reload"),),
        )

        expanded = WorkflowExpander(
            {},
            macro_provider_resolver=lambda capability_id: skill_provider if capability_id.startswith("skill.") else None,
        ).expand(high_level, request=request)

        self.assertEqual([node.capability_id for node in expanded.nodes], ["main_agent.respond"])
        self.assertEqual(expanded.nodes[0].metadata["forced_skill_name"], "demo-hot-reload")
        self.assertEqual(expanded.nodes[0].metadata["skill_bundle_revision"], "skillrev-1")

    def test_skill_executor_mode_expands_to_skill_node_and_finalizer(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "skill"
            skill_dir = root / "scripted"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                """---
name: scripted
description: 处理文本
scripts:
  - name: echo
    path: scripts/echo.py
    runtime: python
---

# Scripted
运行脚本。
""",
                encoding="utf-8",
            )
            state = SkillRuntimeState.from_roots(
                skill_roots=(root,),
                public_skill_roots=(root,),
                reserved_capability_ids=("main_agent.respond",),
            )
            skill_provider = SkillWorkflowProvider(
                skill_name_resolver=lambda capability_id, revision: state.skill_name_for_capability(capability_id, revision),
                skill_manifest_resolver=lambda capability_id, revision: state.catalog_for_revision(revision).get(state.skill_name_for_capability(capability_id, revision) or ""),
            )
            request = OrchestrationRequest(
                task_id="task-skill-executor",
                conversation_id="conv-1",
                root_message_id="msg-1",
                user_message="请处理这个文本",
                metadata={"skill_bundle_revision": state.active_revision},
            )
            high_level = WorkflowPlan(
                task_id="task-skill-executor",
                nodes=(WorkflowNodePlan(node_id="demo", capability_id="skill.scripted"),),
            )

            expanded = WorkflowExpander(
                {},
                macro_provider_resolver=lambda capability_id: skill_provider if capability_id.startswith("skill.") else None,
            ).expand(high_level, request=request)

        self.assertEqual([node.capability_id for node in expanded.nodes], ["skill.scripted", "main_agent.respond"])
        self.assertEqual(expanded.nodes[0].input_payload["user_message"], "请处理这个文本")
        self.assertEqual(expanded.nodes[1].depends_on, (expanded.nodes[0].node_id,))
        self.assertEqual(expanded.nodes[1].metadata["response_role"], RESPONSE_ROLE_FINAL)
        self.assertEqual(expanded.nodes[1].metadata["finalizer_source"], "workflow_expander")
        self.assertFalse(expanded.nodes[1].metadata["auto_skill_matching_enabled"])

    def test_multi_skill_plan_adds_single_global_finalizer_without_intermediate_finalizers(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "skill"
            skill_provider, revision = self._platform_skill_provider(
                root,
                (
                    ("lookup-a", "skill.lookup_a"),
                    ("lookup-b", "skill.lookup_b"),
                ),
            )
            request = OrchestrationRequest(
                task_id="task-multi",
                conversation_id="conv-1",
                root_message_id="msg-1",
                user_message="先查数据A，再查数据B",
                metadata={"skill_bundle_revision": revision},
            )
            high_level = WorkflowPlan(
                task_id="task-multi",
                nodes=(
                    WorkflowNodePlan(node_id="lookup_a", capability_id="skill.lookup_a"),
                    WorkflowNodePlan(node_id="lookup_b", capability_id="skill.lookup_b"),
                ),
            )

            expanded = WorkflowExpander(
                {},
                macro_provider_resolver=lambda capability_id: skill_provider if capability_id.startswith("skill.") else None,
            ).expand(high_level, request=request)

        main_nodes = [node for node in expanded.nodes if node.capability_id == "main_agent.respond"]
        skill_nodes = [node for node in expanded.nodes if node.capability_id.startswith("skill.")]
        final_node = main_nodes[-1]

        self.assertEqual(len(skill_nodes), 2)
        self.assertEqual(len(main_nodes), 1)
        self.assertEqual(final_node.metadata["response_role"], RESPONSE_ROLE_FINAL)
        self.assertEqual(final_node.metadata["finalizer_source"], "workflow_expander")
        self.assertEqual(
            final_node.depends_on,
            (
                "task-multi:lookup_a:skill_execute",
                "task-multi:lookup_b:skill_execute",
            ),
        )

    def test_public_skill_dependencies_are_not_used_to_block_independent_skill_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "skill"
            skill_provider, revision = self._platform_skill_provider(
                root,
                (
                    ("data-lookup", "skill.data_lookup"),
                    ("rcbd-design", "skill.mini_breedstat_rcbd"),
                ),
            )
            request = OrchestrationRequest(
                task_id="task-independent-skills",
                conversation_id="conv-1",
                root_message_id="msg-1",
                user_message="查龙粳33，并按上传材料清单设计随机区组",
                metadata={"skill_bundle_revision": revision},
            )
            high_level = WorkflowPlan(
                task_id="task-independent-skills",
                nodes=(
                    WorkflowNodePlan(node_id="data_lookup_1", capability_id="skill.data_lookup"),
                    WorkflowNodePlan(
                        node_id="rcbd_design_1",
                        capability_id="skill.mini_breedstat_rcbd",
                        depends_on=("data_lookup_1",),
                    ),
                    WorkflowNodePlan(
                        node_id="answer_user",
                        capability_id="main_agent.respond",
                        depends_on=("data_lookup_1", "rcbd_design_1"),
                    ),
                ),
            )

            expanded = WorkflowExpander(
                {},
                macro_provider_resolver=lambda capability_id: skill_provider if capability_id.startswith("skill.") else None,
            ).expand(high_level, request=request)

        lookup_node = next(node for node in expanded.nodes if node.node_id == "task-independent-skills:data_lookup_1:skill_execute")
        rcbd_node = next(node for node in expanded.nodes if node.node_id == "task-independent-skills:rcbd_design_1:skill_execute")
        final_node = next(
            node
            for node in expanded.nodes
            if node.capability_id == "main_agent.respond" and node.metadata.get("response_role") == RESPONSE_ROLE_FINAL
        )

        self.assertEqual(lookup_node.depends_on, ())
        self.assertEqual(rcbd_node.depends_on, ())
        self.assertEqual(
            final_node.depends_on,
            (
                "task-independent-skills:data_lookup_1:skill_execute",
                "task-independent-skills:rcbd_design_1:skill_execute",
            ),
        )
        self.assertEqual(
            expanded.metadata["dropped_public_skill_dependencies"],
            {"rcbd_design_1": ("data_lookup_1",)},
        )

    def test_public_skill_dependency_can_be_explicitly_preserved_for_chained_skills(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "skill"
            skill_provider, revision = self._platform_skill_provider(
                root,
                (
                    ("lookup-a", "skill.lookup_a"),
                    ("lookup-b", "skill.lookup_b"),
                ),
            )
            request = OrchestrationRequest(
                task_id="task-chained-skills",
                conversation_id="conv-1",
                root_message_id="msg-1",
                user_message="先查A，再用A处理B",
                metadata={"skill_bundle_revision": revision},
            )
            high_level = WorkflowPlan(
                task_id="task-chained-skills",
                nodes=(
                    WorkflowNodePlan(node_id="lookup_a", capability_id="skill.lookup_a"),
                    WorkflowNodePlan(
                        node_id="lookup_b",
                        capability_id="skill.lookup_b",
                        input_payload={"requires_public_skill_dependency": True},
                        depends_on=("lookup_a",),
                    ),
                ),
            )

            expanded = WorkflowExpander(
                {},
                macro_provider_resolver=lambda capability_id: skill_provider if capability_id.startswith("skill.") else None,
            ).expand(high_level, request=request)

        lookup_b = next(node for node in expanded.nodes if node.node_id == "task-chained-skills:lookup_b:skill_execute")
        self.assertEqual(lookup_b.depends_on, ("task-chained-skills:lookup_a:skill_execute",))
        self.assertEqual(expanded.metadata["dropped_public_skill_dependencies"], {})

    def test_explicit_task_finalizer_is_marked_final_without_duplicate_global_finalizer(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "skill"
            skill_provider, revision = self._platform_skill_provider(
                root,
                (
                    ("lookup-a", "skill.lookup_a"),
                    ("lookup-b", "skill.lookup_b"),
                ),
            )
            request = OrchestrationRequest(
                task_id="task-explicit-final",
                conversation_id="conv-1",
                root_message_id="msg-1",
                user_message="查两个数据后汇总",
                metadata={"skill_bundle_revision": revision},
            )
            high_level = WorkflowPlan(
                task_id="task-explicit-final",
                nodes=(
                    WorkflowNodePlan(node_id="lookup_a", capability_id="skill.lookup_a"),
                    WorkflowNodePlan(node_id="lookup_b", capability_id="skill.lookup_b"),
                    WorkflowNodePlan(
                        node_id="answer_user",
                        capability_id="main_agent.respond",
                        depends_on=("lookup_a", "lookup_b"),
                    ),
                ),
            )

            expanded = WorkflowExpander(
                {},
                macro_provider_resolver=lambda capability_id: skill_provider if capability_id.startswith("skill.") else None,
            ).expand(high_level, request=request)

        final_nodes = [
            node
            for node in expanded.nodes
            if node.capability_id == "main_agent.respond" and node.metadata.get("response_role") == RESPONSE_ROLE_FINAL
        ]

        self.assertEqual(len(final_nodes), 1)
        self.assertEqual(final_nodes[0].node_id, "answer_user")
        self.assertFalse(expanded.metadata["global_finalizer_added"])

    def test_partial_explicit_answer_does_not_suppress_global_finalizer(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "skill"
            skill_provider, revision = self._platform_skill_provider(
                root,
                (
                    ("lookup-a", "skill.lookup_a"),
                    ("lookup-b", "skill.lookup_b"),
                ),
            )
            request = OrchestrationRequest(
                task_id="task-partial-final",
                conversation_id="conv-1",
                root_message_id="msg-1",
                user_message="查两个数据，其中一个先解释",
                metadata={"skill_bundle_revision": revision},
            )
            high_level = WorkflowPlan(
                task_id="task-partial-final",
                nodes=(
                    WorkflowNodePlan(node_id="lookup_a", capability_id="skill.lookup_a"),
                    WorkflowNodePlan(node_id="answer_a", capability_id="main_agent.respond", depends_on=("lookup_a",)),
                    WorkflowNodePlan(node_id="lookup_b", capability_id="skill.lookup_b"),
                ),
            )

            expanded = WorkflowExpander(
                {},
                macro_provider_resolver=lambda capability_id: skill_provider if capability_id.startswith("skill.") else None,
            ).expand(high_level, request=request)

        final_nodes = [
            node
            for node in expanded.nodes
            if node.capability_id == "main_agent.respond" and node.metadata.get("response_role") == RESPONSE_ROLE_FINAL
        ]

        self.assertEqual(len(final_nodes), 1)
        self.assertEqual(final_nodes[0].node_id, "task-partial-final:global_final_answer")
        self.assertTrue(expanded.metadata["global_finalizer_added"])
        self.assertIn("task-partial-final:lookup_b:skill_execute", final_nodes[0].depends_on)

    def test_skill_direct_answer_mode_does_not_add_finalizer(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "skill"
            skill_dir = root / "direct"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                """---
name: direct
description: 直接回答
scripts:
  - name: echo
    path: scripts/echo.py
    runtime: python
execution:
  answer_mode: direct
---

# Direct
直接回答。
""",
                encoding="utf-8",
            )
            state = SkillRuntimeState.from_roots(
                skill_roots=(root,),
                public_skill_roots=(root,),
                reserved_capability_ids=("main_agent.respond",),
            )
            skill_provider = SkillWorkflowProvider(
                skill_name_resolver=lambda capability_id, revision: state.skill_name_for_capability(capability_id, revision),
                skill_manifest_resolver=lambda capability_id, revision: state.catalog_for_revision(revision).get(state.skill_name_for_capability(capability_id, revision) or ""),
            )
            request = OrchestrationRequest(
                task_id="task-skill-direct",
                conversation_id="conv-1",
                root_message_id="msg-1",
                user_message="请直接回答",
                metadata={"skill_bundle_revision": state.active_revision},
            )
            high_level = WorkflowPlan(
                task_id="task-skill-direct",
                nodes=(WorkflowNodePlan(node_id="demo", capability_id="skill.direct"),),
            )

            expanded = WorkflowExpander(
                {},
                macro_provider_resolver=lambda capability_id: skill_provider if capability_id.startswith("skill.") else None,
            ).expand(high_level, request=request)

        self.assertEqual([node.capability_id for node in expanded.nodes], ["skill.direct"])


if __name__ == "__main__":
    unittest.main()
