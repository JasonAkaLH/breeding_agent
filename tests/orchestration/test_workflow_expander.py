from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.capabilities.sql_query import SQLQueryWorkflowProvider
from src.orchestration.models import OrchestrationRequest, WorkflowNodePlan, WorkflowPlan
from src.orchestration.skill_workflow_provider import SkillWorkflowProvider
from src.orchestration.workflow_expander import WorkflowExpander
from src.integrations.codex_skills.skill_runtime_state import SkillRuntimeState


class WorkflowExpanderTest(unittest.TestCase):
    def test_expands_sql_query_macro_and_rewires_downstream_dependencies(self) -> None:
        request = OrchestrationRequest(
            task_id="task-1",
            conversation_id="conv-1",
            root_message_id="msg-1",
            user_message="查询龙粳33的基因型信息",
        )
        high_level = WorkflowPlan(
            task_id="task-1",
            nodes=(
                WorkflowNodePlan(node_id="query_data", capability_id="sql_query.query"),
                WorkflowNodePlan(node_id="answer_user", capability_id="main_agent.respond", depends_on=("query_data",)),
            ),
        )

        expanded = WorkflowExpander({"sql_query.query": SQLQueryWorkflowProvider()}).expand(high_level, request=request)

        self.assertEqual(expanded.task_id, "task-1")
        self.assertEqual(len(expanded.nodes), 7)
        capability_ids = [node.capability_id for node in expanded.nodes]
        self.assertEqual(capability_ids[:6], [
            "sql_query.intent_route",
            "sql_query.schema_context_prepare",
            "sql_query.sql_generate",
            "sql_query.sql_guard",
            "sql_query.sql_execute_readonly",
            "sql_query.result_filtering",
        ])
        self.assertEqual(capability_ids[-1], "main_agent.respond")
        self.assertIn("task-1:query_data:result_filtering", expanded.nodes[-1].depends_on)
        self.assertNotIn("query_data", expanded.nodes[-1].depends_on)

    def test_macro_roots_depend_on_high_level_dependencies(self) -> None:
        request = OrchestrationRequest(
            task_id="task-2",
            conversation_id="conv-1",
            root_message_id="msg-1",
            user_message="查询龙粳33",
        )
        high_level = WorkflowPlan(
            task_id="task-2",
            nodes=(
                WorkflowNodePlan(node_id="prepare_context", capability_id="main_agent.respond"),
                WorkflowNodePlan(node_id="query_data", capability_id="sql_query.query", depends_on=("prepare_context",)),
            ),
        )

        expanded = WorkflowExpander({"sql_query.query": SQLQueryWorkflowProvider()}).expand(high_level, request=request)
        intent_node = next(node for node in expanded.nodes if node.capability_id == "sql_query.intent_route")

        self.assertEqual(intent_node.depends_on, ("prepare_context",))

    def test_expanded_plan_inherits_macro_replan_budget(self) -> None:
        request = OrchestrationRequest(
            task_id="task-budget",
            conversation_id="conv-1",
            root_message_id="msg-1",
            user_message="查询龙粳33",
        )
        high_level = WorkflowPlan(
            task_id="task-budget",
            nodes=(WorkflowNodePlan(node_id="query_data", capability_id="sql_query.query"),),
            max_replans=0,
            max_dynamic_nodes=0,
        )

        expanded = WorkflowExpander({"sql_query.query": SQLQueryWorkflowProvider()}).expand(high_level, request=request)

        self.assertGreaterEqual(expanded.max_replans, 1)
        self.assertGreaterEqual(expanded.max_dynamic_nodes, 12)

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
                reserved_capability_ids=("main_agent.respond", "sql_query.query"),
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
                reserved_capability_ids=("main_agent.respond", "sql_query.query"),
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
