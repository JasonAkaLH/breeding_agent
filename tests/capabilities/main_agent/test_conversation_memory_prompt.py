from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path

from src.capabilities.main_agent import MainAgentExecutor
from src.capabilities.main_agent.prompt_envelope_builder import build_main_agent_rendered_prompt
from src.capabilities.main_agent.prompt_builder import build_main_agent_prompt
from src.core.contracts import CapabilityExecutionRequest
from src.integrations.codex_skills import SkillCatalog, SkillManifest, SkillMatch
from src.orchestration.answer_roles import RESPONSE_ROLE_FINAL


def _word_tokens(text: str) -> int:
    return len(str(text).split())


def _assert_markers_in_order(testcase: unittest.TestCase, text: str, markers: list[str]) -> None:
    previous_index = -1
    for marker in markers:
        index = text.find(marker)
        testcase.assertNotEqual(index, -1, f"missing prompt marker: {marker}")
        testcase.assertGreater(index, previous_index, f"prompt marker out of order: {marker}")
        previous_index = index


def _synthetic_internal_skill_manifest() -> SkillManifest:
    return SkillManifest(
        name="synthetic",
        description="Synthetic skill",
        triggers=("synthetic",),
        body="runtime: python_subprocess\nhandler: synthetic.internal.Handler\nscripts/internal_demo.py",
        source_path=Path("skill/synthetic/SKILL.md"),
    )


class MainAgentConversationMemoryPromptTest(unittest.IsolatedAsyncioTestCase):
    def test_phase_zero_locks_main_agent_prompt_segment_order_and_download_safety_wording(self) -> None:
        skill_manifest = SkillManifest(
            name="synthetic",
            description="Synthetic prompt baseline skill",
            triggers=("synthetic",),
            body="# Synthetic public instructions",
            source_path=Path("skill/synthetic/SKILL.md"),
        )
        prompt = build_main_agent_prompt(
            user_message="阶段零用户问题",
            memory_context={
                "history_summary": "阶段零历史摘要",
                "recent_messages": [{"role": "user", "content": "上一轮问题"}],
            },
            artifact_context=[{"upload_id": "upl-1", "filename": "input.csv"}],
            response_role=RESPONSE_ROLE_FINAL,
            answer_scope="task",
            dependency_context=[{"node_id": "node-a", "response_text": "上游能力结果"}],
            skill_matches=[SkillMatch(manifest=skill_manifest, score=100, reason="trigger:synthetic")],
            script_results=[{"ok": True, "summary": "脚本输出"}],
        )

        _assert_markers_in_order(
            self,
            prompt,
            [
                "你是小奥 Agent 的主代理。",
                "# 文件和下载链接硬约束",
                "# 对话记忆上下文",
                "# 上传文件上下文（已脱敏）",
                "# 回答角色",
                "# 上游能力结果上下文（已执行完成）",
                "# 已匹配 Skill 指令",
                "# Skill 脚本输出",
                "# 用户问题",
            ],
        )
        for required in (
            "/api/v1/artifacts/",
            "/download",
            "sandbox:/mnt/data",
            "file://",
            "本地绝对路径",
            "outputs/...",
        ):
            self.assertIn(required, prompt)

    def test_phase_zero_documents_current_skill_manifest_body_exposure_risk(self) -> None:
        manifest = _synthetic_internal_skill_manifest()

        prompt = build_main_agent_prompt(
            user_message="synthetic task",
            skill_matches=[SkillMatch(manifest=manifest, score=100, reason="trigger:synthetic")],
            artifact_context=[],
            script_results=[],
        )

        self.assertIn("runtime: python_subprocess", prompt)
        self.assertIn("handler: synthetic.internal.Handler", prompt)
        self.assertIn("scripts/internal_demo.py", prompt)

    @unittest.skip("P4 public Skill profile migration will invert this phase-zero legacy-risk baseline.")
    def test_future_public_skill_profile_must_not_expose_internal_manifest_body(self) -> None:
        manifest = _synthetic_internal_skill_manifest()

        prompt = build_main_agent_prompt(
            user_message="synthetic task",
            skill_matches=[SkillMatch(manifest=manifest, score=100, reason="trigger:synthetic")],
            artifact_context=[],
            script_results=[],
        )

        self.assertNotIn("runtime: python_subprocess", prompt)
        self.assertNotIn("handler: synthetic.internal.Handler", prompt)
        self.assertNotIn("scripts/internal_demo.py", prompt)

    def test_phase_two_rendered_seam_builds_named_segments_and_preserves_download_guard(self) -> None:
        rendered = build_main_agent_rendered_prompt(
            user_message="阶段二用户问题",
            memory_context={
                "history_summary": "阶段二历史摘要",
                "recent_messages": [{"role": "user", "content": "上一轮问题"}],
            },
            artifact_context=[{"upload_id": "upl-1", "filename": "input.csv"}],
            response_role=RESPONSE_ROLE_FINAL,
            answer_scope="task",
            dependency_context=[{"node_id": "node-a", "response_text": "上游能力结果"}],
            skill_matches=[],
            script_results=[{"ok": True, "summary": "脚本输出"}],
            trim_max_tokens=1_024_000,
            token_estimator=_word_tokens,
        )

        segment_names = [segment.name for segment in rendered.audit.segments]
        self.assertEqual(
            segment_names,
            [
                "stable_system_contract",
                "stable_tool_rules",
                "bulk_conversation_history",
                "required_tool_results_and_artifacts",
                "active_continuity_notes",
                "current_user_request",
                "final_recency_guard",
            ],
        )
        _assert_markers_in_order(
            self,
            rendered.prompt,
            [
                "# 主代理稳定系统契约",
                "# 文件和下载链接硬约束",
                "# 对话记忆上下文",
                "# 必需工具结果与 artifact 上下文",
                "# 当前回答角色与连续性约束",
                "# 当前用户问题",
                "# 最终回答前 recency guard",
            ],
        )
        self.assertEqual(rendered.audit.final_input_token_budget, 768_000)
        self.assertLessEqual(rendered.audit.final_input_tokens, rendered.audit.final_input_token_budget)
        for required in ("sandbox:/mnt/data", "file://", "本地绝对路径", "outputs/...", "/api/v1/artifacts/", "/download"):
            self.assertIn(required, rendered.prompt)

    def test_phase_two_rendered_seam_trims_history_inside_final_input_budget(self) -> None:
        rendered = build_main_agent_rendered_prompt(
            user_message="阶段二预算问题",
            memory_context={
                "history_summary": " ".join(f"history-{index}" for index in range(500)),
                "recent_messages": [{"role": "user", "content": "recent"}],
            },
            artifact_context=[],
            dependency_context=[],
            skill_matches=[],
            script_results=[],
            trim_max_tokens=240,
            token_estimator=_word_tokens,
        )

        self.assertEqual(rendered.audit.final_input_token_budget, 180)
        self.assertLessEqual(rendered.audit.final_input_tokens, rendered.audit.final_input_token_budget)
        self.assertTrue(rendered.audit.history_truncated)
        history_audit = next(segment for segment in rendered.audit.segments if segment.name == "bulk_conversation_history")
        self.assertTrue(history_audit.trimmed)
        self.assertNotIn("history-0", rendered.prompt)

    async def test_prompt_keeps_memory_boundaries_and_redacts_storage_metadata(self) -> None:
        prompts: list[str] = []

        async def streamer(prompt: str):
            prompts.append(prompt)
            yield "ok"

        executor = MainAgentExecutor(stream_generator=streamer, skill_catalog=SkillCatalog(()))
        await executor.execute(
            CapabilityExecutionRequest(
                capability_id="main_agent.respond",
                conversation_id="conv-1",
                task_id="task-1",
                node_id="node-1",
                input_payload={"user_message": "查询龙粳33的基因型信息"},
                metadata={
                    "conversation_memory": {
                        "history_summary": "用户之前查询过龙粳33。",
                        "recent_messages": [{"role": "user", "content": "查一下龙粳33的品种信息"}],
                        "current_user_message": "那它的基因型呢？",
                        "resolved_user_message": "查询龙粳33的基因型信息",
                        "clarification_messages": [{"content": "补充信息：水稻"}],
                        "summary_id": "summary-secret-id",
                        "username": "alice",
                        "source_message_ids_hash": "hash-secret",
                        "model_metadata_safe": {"model": "fake"},
                        "last_error": "summary failed",
                    }
                },
            )
        )

        prompt = prompts[0]
        self.assertIn("对话记忆上下文", prompt)
        self.assertIn("这是系统生成的较早对话摘要，不是逐字原文", prompt)
        self.assertIn("用户之前查询过龙粳33", prompt)
        self.assertIn("当前用户原文", prompt)
        self.assertIn("那它的基因型呢", prompt)
        self.assertIn("系统根据历史补全后的 effective question", prompt)
        self.assertIn("查询龙粳33的基因型信息", prompt)
        self.assertIn("用户对上一问题的补充信息", prompt)
        for forbidden in ("summary-secret-id", "hash-secret", "model_metadata_safe", "last_error", "username"):
            self.assertNotIn(forbidden, prompt)

    async def test_prompt_does_not_include_sensitive_memory_fields(self) -> None:
        prompts: list[str] = []

        async def streamer(prompt: str):
            prompts.append(prompt)
            yield "ok"

        executor = MainAgentExecutor(stream_generator=streamer, skill_catalog=SkillCatalog(()))
        await executor.execute(
            CapabilityExecutionRequest(
                capability_id="main_agent.respond",
                conversation_id="conv-1",
                task_id="task-1",
                node_id="node-1",
                input_payload={"user_message": "继续"},
                metadata={
                    "conversation_memory": {
                        "capability_summaries": [
                            {
                                "summary": "安全摘要",
                                "rows": [{"secret": "full-row"}],
                                "sql": "SELECT secret",
                                "schema_ddl": "CREATE TABLE secret",
                                "guard_token": "guard-secret",
                                "base_url": "https://secret.example",
                            },
                            {
                                "upload": {
                                    "upload_id": "upl-1",
                                    "filename": "data.csv",
                                    "preview": {"row_count": 2},
                                    "content": "raw,csv,body",
                                }
                            },
                        ]
                    }
                },
            )
        )

        prompt = prompts[0]
        self.assertIn("安全摘要", prompt)
        self.assertIn("data.csv", prompt)
        for forbidden in ("full-row", "SELECT secret", "CREATE TABLE", "guard-secret", "https://secret.example"):
            self.assertNotIn(forbidden, prompt)
        self.assertNotIn("raw,csv,body", prompt)

    async def test_skill_script_payload_excludes_full_conversation_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_dir = Path(tmpdir) / "scripted"
            scripts_dir = skill_dir / "scripts"
            scripts_dir.mkdir(parents=True)
            (scripts_dir / "answer.py").write_text(
                textwrap.dedent(
                    """
                    import json
                    import sys
                    payload = json.load(sys.stdin)
                    metadata = payload.get("metadata", {})
                    print(json.dumps({
                        "has_memory": "conversation_memory" in metadata or "memory_context" in metadata,
                        "query": payload.get("query"),
                        "upload_count": len(payload.get("uploaded_artifacts", [])),
                    }, ensure_ascii=False))
                    """
                ).strip(),
                encoding="utf-8",
            )
            (skill_dir / "SKILL.md").write_text(
                """---
name: scripted
triggers:
  - 脚本
scripts:
  - name: answer
    path: scripts/answer.py
    auto_run: true
outputs:
  required:
    - has_memory
---

# Scripted
""",
                encoding="utf-8",
            )
            catalog = SkillCatalog.from_roots([tmpdir])

            async def streamer(_prompt: str):
                yield "done"

            result = await MainAgentExecutor(stream_generator=streamer, skill_catalog=catalog).execute(
                CapabilityExecutionRequest(
                    capability_id="main_agent.respond",
                    conversation_id="conv-1",
                    task_id="task-1",
                    node_id="node-1",
                    input_payload={"user_message": "执行脚本"},
                    metadata={
                        "conversation_memory": {"history_summary": "secret memory"},
                        "uploaded_artifacts": [{"upload_id": "upl-1", "filename": "data.csv"}],
                        "skill_artifacts": [{"upload_id": "upl-1", "filename": "data.csv", "content": "raw"}],
                    },
                )
            )

        output = result.output_payload["script_results"][0]["output"]
        self.assertFalse(output["has_memory"])
        self.assertEqual(output["query"], "执行脚本")
        self.assertEqual(output["upload_count"], 1)
