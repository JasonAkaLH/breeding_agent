from __future__ import annotations

import os
import json
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from src.capabilities.main_agent import MainAgentExecutor, MainAgentWorkflowProvider
from src.capabilities.main_agent.prompt_builder import build_main_agent_prompt
from src.core.contracts import CapabilityExecutionRequest
from src.orchestration.answer_roles import RESPONSE_ROLE_FINAL
from src.orchestration.prompt_envelope import LLMMessage
from src.orchestration.models import OrchestrationRequest
from src.integrations.agent_skills import SkillCatalog


async def _collecting_streamer(prompt: str):
    yield "hello"
    yield " world"


class MainAgentWorkflowAndExecutorTest(unittest.IsolatedAsyncioTestCase):
    async def test_workflow_provider_builds_single_main_agent_node(self) -> None:
        provider = MainAgentWorkflowProvider()
        plan = provider.build_plan(
            OrchestrationRequest(
                task_id="task-1",
                conversation_id="conv-1",
                root_message_id="msg-1",
                user_message="你好",
            )
        )

        self.assertEqual(plan.metadata["route"], "main_agent")
        self.assertEqual(len(plan.nodes), 1)
        self.assertEqual(plan.nodes[0].capability_id, "main_agent.respond")
        self.assertEqual(plan.nodes[0].input_payload["user_message"], "你好")

    async def test_workflow_provider_allows_replan_budget_only_for_soft_skill_binding(self) -> None:
        provider = MainAgentWorkflowProvider()
        plan = provider.build_plan(
            OrchestrationRequest(
                task_id="task-soft",
                conversation_id="conv-1",
                root_message_id="msg-1",
                user_message="请执行",
                requested_capability_id="main_agent.respond",
                metadata={"soft_skill_binding": {"capability_id": "skill.demo", "skill_bundle_revision": "skillrev-1"}},
            )
        )

        self.assertEqual(plan.nodes[0].capability_id, "main_agent.respond")
        self.assertEqual(plan.nodes[0].metadata["soft_skill_binding"]["capability_id"], "skill.demo")
        self.assertEqual(plan.max_replans, 1)
        self.assertEqual(plan.max_dynamic_nodes, 4)

    async def test_executor_streams_answer_chunks_and_returns_text_artifact(self) -> None:
        seen_prompts: list[str] = []
        transient_events = []

        async def streamer(prompt: str):
            seen_prompts.append(prompt)
            yield "hello"
            yield " world"

        async def publish_transient(event):
            transient_events.append(event)

        executor = MainAgentExecutor(
            stream_generator=streamer,
            skill_catalog=SkillCatalog(()),
            transient_event_publisher=publish_transient,
        )
        result = await executor.execute(
            CapabilityExecutionRequest(
                capability_id="main_agent.respond",
                conversation_id="conv-1",
                task_id="task-1",
                node_id="node-1",
                input_payload={"user_message": "你好"},
                metadata={"uploaded_artifacts": [{"artifact_id": "art-1", "filename": "a.txt", "content": "secret"}]},
            )
        )

        self.assertEqual(result.output_payload["response_text"], "hello world")
        self.assertEqual(result.output_payload["response_source"], "llm")
        self.assertEqual(result.artifacts[0].artifact_type.value, "text")
        self.assertIn("第一性原理", seen_prompts[0])
        self.assertIn("不要假定用户每次都知道自己要什么", seen_prompts[0])
        self.assertIn("a.txt", seen_prompts[0])
        self.assertNotIn("secret", seen_prompts[0])
        self.assertEqual([event.event_type for event in transient_events], ["main_agent.output_delta", "main_agent.output_delta"])
        self.assertFalse(any(event.event_type == "main_agent.output_delta" for event in result.events))

    async def test_soft_skill_binding_answer_uses_public_profile_without_running_scripts_or_raw_body(self) -> None:
        seen_prompts: list[str] = []
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            skill_dir = root / "demo"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                textwrap.dedent(
                    """\
                    ---
                    name: demo-skill
                    capability_id: skill.demo
                    display_name: 演示 Skill
                    description: 解释公开用法。
                    public_usage:
                      overview: 公开字段说明
                      input_formats:
                        - name: demo_data
                          description: CSV 表格。
                      examples:
                        - /demo 怎么填 demo_data？
                      outputs:
                        - 公开结果
                    scripts:
                      - name: run_demo
                        path: scripts/run_demo.py
                        runtime: python
                    ---
                    # Internal body
                    scripts/run_demo.py Rscript wrapper handler secret details.
                    """
                ),
                encoding="utf-8",
            )
            catalog = SkillCatalog.from_roots((root,))

        async def streamer(prompt: str):
            seen_prompts.append(prompt)
            if "Skill 软绑定判断器" in prompt:
                yield '{"decision":"answer","target_capability_id":"skill.demo","reason_code":"usage_question"}'
            else:
                yield "demo_data 需要"
                yield "上传 CSV 表格。"

        transient_events = []

        async def publish_transient(event):
            transient_events.append(event)

        executor = MainAgentExecutor(
            stream_generator=streamer,
            skill_catalog=catalog,
            transient_event_publisher=publish_transient,
        )
        result = await executor.execute(
            CapabilityExecutionRequest(
                capability_id="main_agent.respond",
                conversation_id="conv-1",
                task_id="task-1",
                node_id="node-1",
                input_payload={"user_message": "demo_data 怎么填？"},
                metadata={"soft_skill_binding": {"capability_id": "skill.demo"}},
            )
        )

        self.assertEqual(result.output_payload["response_source"], "llm")
        self.assertEqual(result.output_payload["response_text"], "demo_data 需要上传 CSV 表格。")
        self.assertNotIn("soft_skill_decision", result.output_payload)
        self.assertEqual([event.event_type for event in result.events], ["soft_skill_binding.decision", "main_agent.output_final"])
        self.assertEqual(
            [event.payload["delta"] for event in transient_events if event.event_type == "main_agent.output_delta"],
            ["demo_data 需要", "上传 CSV 表格。"],
        )
        final_event = next(event for event in result.events if event.event_type == "main_agent.output_final")
        self.assertEqual(final_event.payload["answer_chunk_count"], 2)
        self.assertIn("公开字段说明", seen_prompts[0])
        self.assertIn("公开字段说明", seen_prompts[1])
        combined_prompts = "\n".join(seen_prompts)
        self.assertNotIn("scripts/run_demo.py", combined_prompts)
        self.assertNotIn("Rscript", combined_prompts)
        self.assertNotIn("handler", combined_prompts)

    async def test_soft_skill_binding_execute_returns_deterministic_replan_signal_without_user_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            skill_dir = root / "demo"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                textwrap.dedent(
                    """\
                    ---
                    name: demo-skill
                    capability_id: skill.demo
                    description: 执行演示。
                    public_usage:
                      overview: 执行演示。
                      input_formats:
                        - name: demo_data
                          description: CSV 表格。
                      examples:
                        - /demo 执行
                      outputs:
                        - 结果
                    ---
                    Body.
                    """
                ),
                encoding="utf-8",
            )
            catalog = SkillCatalog.from_roots((root,))

        async def streamer(_prompt: str):
            yield '{"decision":"execute","target_capability_id":"skill.demo","confidence":0.94,"reason_code":"ready_to_execute"}'

        transient_events = []

        async def publish_transient(event):
            transient_events.append(event)

        executor = MainAgentExecutor(
            stream_generator=streamer,
            skill_catalog=catalog,
            transient_event_publisher=publish_transient,
        )
        result = await executor.execute(
            CapabilityExecutionRequest(
                capability_id="main_agent.respond",
                conversation_id="conv-1",
                task_id="task-1",
                node_id="node-1",
                input_payload={"user_message": "执行"},
                metadata={"soft_skill_binding": {"capability_id": "skill.demo"}},
            )
        )

        self.assertEqual(result.output_payload["response_source"], "soft_skill_decision")
        self.assertEqual(result.output_payload["soft_skill_decision"]["decision"], "execute")
        self.assertTrue(result.output_payload["satisfaction"]["replan_recommended"])
        self.assertEqual(result.artifacts, ())
        self.assertFalse(transient_events)
        self.assertEqual([event.event_type for event in result.events], ["soft_skill_binding.decision"])

    async def test_soft_skill_binding_low_confidence_execute_downgrades_to_answer(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            skill_dir = root / "demo"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                textwrap.dedent(
                    """\
                    ---
                    name: demo-skill
                    capability_id: skill.demo
                    description: 执行演示。
                    public_usage:
                      overview: 公开字段说明
                      input_formats:
                        - name: demo_data
                          description: CSV 表格。
                      examples:
                        - /demo 执行
                      outputs:
                        - 结果
                    ---
                    Raw body.
                    """
                ),
                encoding="utf-8",
            )
            catalog = SkillCatalog.from_roots((root,))

        async def streamer(prompt: str):
            if "Skill 软绑定判断器" in prompt:
                yield '{"decision":"execute","target_capability_id":"skill.demo","confidence":"low","reason_code":"unclear"}'
            else:
                yield "请先补充 demo_data。"

        executor = MainAgentExecutor(stream_generator=streamer, skill_catalog=catalog)
        result = await executor.execute(
            CapabilityExecutionRequest(
                capability_id="main_agent.respond",
                conversation_id="conv-1",
                task_id="task-1",
                node_id="node-1",
                input_payload={"user_message": "帮我做一下"},
                metadata={"soft_skill_binding": {"capability_id": "skill.demo"}},
            )
        )

        self.assertEqual(result.output_payload["response_source"], "llm")
        self.assertEqual(result.output_payload["response_text"], "请先补充 demo_data。")
        self.assertNotIn("soft_skill_decision", result.output_payload)
        decision_event = next(event for event in result.events if event.event_type == "soft_skill_binding.decision")
        self.assertEqual(decision_event.payload["reason_code"], "low_confidence")

    async def test_soft_skill_binding_answer_uses_conversation_memory_for_followup(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            skill_dir = root / "demo"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                textwrap.dedent(
                    """\
                    ---
                    name: demo-skill
                    capability_id: skill.demo
                    description: 解释公开用法。
                    public_usage:
                      overview: 公开字段说明
                      examples:
                        - /demo 怎么填？
                    ---
                    Raw body.
                    """
                ),
                encoding="utf-8",
            )
            catalog = SkillCatalog.from_roots((root,))

        seen_prompts: list[str] = []

        async def streamer(prompt: str, *, stage: str | None = None):
            seen_prompts.append(prompt)
            if stage == "soft_skill_decision":
                yield '{"decision":"answer","target_capability_id":"skill.demo","confidence":0.9,"reason_code":"followup"}'
            else:
                yield "继续解释。"

        executor = MainAgentExecutor(stream_generator=streamer, skill_catalog=catalog)
        result = await executor.execute(
            CapabilityExecutionRequest(
                capability_id="main_agent.respond",
                conversation_id="conv-1",
                task_id="task-1",
                node_id="node-1",
                input_payload={"user_message": "再说清楚一点"},
                metadata={
                    "soft_skill_binding": {"capability_id": "skill.demo"},
                    "conversation_memory": {
                        "recent_messages": [
                            {"role": "user", "content": "/demo demo_data 怎么填？"},
                            {"role": "assistant", "content": "demo_data 是需要上传的 CSV 表格。"},
                        ]
                    },
                },
            )
        )

        self.assertEqual(result.output_payload["response_text"], "继续解释。")
        self.assertEqual(len(seen_prompts), 2)
        for prompt in seen_prompts:
            self.assertIn("对话记忆上下文", prompt)
            self.assertIn("/demo demo_data 怎么填？", prompt)
            self.assertIn("demo_data 是需要上传的 CSV 表格。", prompt)

    async def test_executor_injects_dependency_outputs_into_prompt(self) -> None:
        seen_prompts: list[str] = []

        async def streamer(prompt: str):
            seen_prompts.append(prompt)
            yield "这是整理后的答案"

        executor = MainAgentExecutor(stream_generator=streamer, skill_catalog=SkillCatalog(()))
        result = await executor.execute(
            CapabilityExecutionRequest(
                capability_id="main_agent.respond",
                conversation_id="conv-1",
                task_id="task-1",
                node_id="node-1",
                input_payload={"user_message": "龙粳18详细信息"},
                dependency_outputs={
                    "task-1:query_data:execute_query": {
                        "columns": ["variety_name", "approval_num"],
                        "rows": [{"variety_name": "龙粳18", "approval_num": "黑审稻"}],
                        "route_id": "dataset_a",
                        "row_count": 1,
                        "preview_row_count": 1,
                        "truncated": False,
                    }
                },
            )
        )

        self.assertEqual(result.output_payload["response_text"], "这是整理后的答案")
        self.assertIn("上游能力结果上下文", seen_prompts[0])
        self.assertIn("dataset_a", seen_prompts[0])
        self.assertIn("columns", seen_prompts[0])
        self.assertIn("rows", seen_prompts[0])
        self.assertIn("龙粳18", seen_prompts[0])

    async def test_executor_streams_reasoning_content_as_separate_frontend_events(self) -> None:
        transient_events = []

        async def streamer(prompt: str, *, reasoning_effort: str = "minimal", thinking: bool = False):
            yield {"reasoning": "先分析问题。", "answer": None}
            yield {"answer": "最终回答", "reasoning": None}

        async def publish_transient(event):
            transient_events.append(event)

        executor = MainAgentExecutor(
            stream_generator=streamer,
            skill_catalog=SkillCatalog(()),
            transient_event_publisher=publish_transient,
        )
        result = await executor.execute(
            CapabilityExecutionRequest(
                capability_id="main_agent.respond",
                conversation_id="conv-1",
                task_id="task-1",
                node_id="node-1",
                input_payload={"user_message": "请深度思考"},
                metadata={"deep_thinking": True},
            )
        )

        reasoning_events = [event for event in transient_events if event.event_type == "main_agent.reasoning_delta"]
        answer_events = [event for event in transient_events if event.event_type == "main_agent.output_delta"]

        self.assertEqual(result.output_payload["response_text"], "最终回答")
        self.assertEqual(result.artifacts[0].storage_ref, "最终回答")
        self.assertEqual([event.payload["delta"] for event in reasoning_events], ["先分析问题。"])
        self.assertEqual([event.payload["delta"] for event in answer_events], ["最终回答"])

    async def test_default_llm_binding_uses_thinking_stream_for_reasoning_content(self) -> None:
        class FakeLLMClient:
            def safe_metadata(self, *, config_source: str | None = None, reasoning_effort: str | None = None) -> dict:
                return {
                    "provider": "openai_compatible",
                    "model": "fake-model",
                    "config_source": config_source,
                    "reasoning_effort": reasoning_effort,
                }

            async def generate_text_with_thinking(
                self,
                prompt: str,
                thinking: bool = False,
                reasoning_effort: str = "high",
            ):
                yield {"reasoning": "默认绑定思考", "answer": None}
                yield {"answer": "默认绑定回答", "reasoning": None}

        with patch("src.capabilities.main_agent.executor.LLMClient", return_value=FakeLLMClient()):
            transient_events = []

            async def publish_transient(event):
                transient_events.append(event)

            executor = MainAgentExecutor(
                skill_catalog=SkillCatalog(()),
                transient_event_publisher=publish_transient,
            )
            result = await executor.execute(
                CapabilityExecutionRequest(
                    capability_id="main_agent.respond",
                    conversation_id="conv-1",
                    task_id="task-1",
                    node_id="node-1",
                    input_payload={"user_message": "请深度思考"},
                    metadata={"deep_thinking": True},
                )
            )

        reasoning_events = [event for event in transient_events if event.event_type == "main_agent.reasoning_delta"]
        self.assertEqual([event.payload["delta"] for event in reasoning_events], ["默认绑定思考"])
        self.assertFalse(any(event.event_type == "main_agent.reasoning_delta" for event in result.events))
        self.assertEqual(result.output_payload["response_text"], "默认绑定回答")

    async def test_executor_records_safe_llm_metadata_on_success(self) -> None:
        async def streamer(prompt: str):
            yield "hello"

        executor = MainAgentExecutor(
            stream_generator=streamer,
            stream_metadata={
                "provider": "openai_compatible",
                "model": "test-model",
                "reasoning_effort": "minimal",
                "config_source": "injected_config",
                "api_key": "must-not-leak",
            },
            skill_catalog=SkillCatalog(()),
        )

        result = await executor.execute(
            CapabilityExecutionRequest(
                capability_id="main_agent.respond",
                conversation_id="conv-1",
                task_id="task-1",
                node_id="node-1",
                input_payload={"user_message": "你好"},
            )
        )

        llm_event = next(event for event in result.events if event.event_type == "main_agent.llm_call")
        self.assertEqual(llm_event.payload["model"], "test-model")
        self.assertEqual(llm_event.payload["reasoning_effort"], "minimal")
        self.assertEqual(llm_event.payload["config_source"], "injected_config")
        self.assertNotIn("api_key", llm_event.payload)

    async def test_prompt_envelope_shadow_keeps_legacy_prompt_and_records_audit_event(self) -> None:
        prompts: list[str] = []

        async def streamer(prompt: str):
            prompts.append(prompt)
            yield "shadow answer"

        request = CapabilityExecutionRequest(
            capability_id="main_agent.respond",
            conversation_id="conv-1",
            task_id="task-shadow",
            node_id="node-shadow",
            input_payload={"user_message": "shadow 用户问题"},
            metadata={"auto_skill_matching_enabled": False},
        )
        executor = MainAgentExecutor(
            stream_generator=streamer,
            stream_metadata={"provider": "injected_stream", "model": "test-model", "trim_max_tokens": 2_000},
            skill_catalog=SkillCatalog(()),
        )

        with patch.dict(os.environ, {"MAF_PROMPT_ENVELOPE_MODE": "shadow"}):
            result = await executor.execute(request)

        legacy_prompt = build_main_agent_prompt(
            user_message="shadow 用户问题",
            skill_matches=[],
            artifact_context=[],
            script_results=[],
            dependency_context=[],
            memory_context={},
        )
        self.assertEqual(prompts, [legacy_prompt])
        prompt_event = next(event for event in result.events if event.event_type == "main_agent.prompt_envelope_rendered")
        self.assertEqual(prompt_event.visibility, "audit_only")
        self.assertEqual(prompt_event.payload["mode"], "shadow")
        self.assertEqual(prompt_event.payload["effective_mode"], "shadow")
        self.assertEqual(prompt_event.payload["final_input_token_budget"], 1_500)
        self.assertEqual(prompt_event.payload["prompt_render_metrics"]["mode"], "shadow")
        self.assertEqual(prompt_event.payload["prompt_render_metrics"]["final_input_token_budget"], 1_500)
        self.assertIn("stable_system_contract", [segment["name"] for segment in prompt_event.payload["segments"]])
        self.assertNotIn("shadow 用户问题", str(prompt_event.payload))
        llm_event = next(event for event in result.events if event.event_type == "main_agent.llm_call")
        self.assertEqual(llm_event.payload["prompt_envelope"]["mode"], "shadow")
        self.assertEqual(llm_event.payload["prompt_envelope"]["effective_mode"], "shadow")
        self.assertEqual(llm_event.payload["prompt_envelope"]["prompt_render_metrics"], prompt_event.payload["prompt_render_metrics"])

    async def test_prompt_envelope_string_sends_envelope_prompt_without_skill_matches(self) -> None:
        prompts: list[str] = []

        async def streamer(prompt: str):
            prompts.append(prompt)
            yield "string answer"

        executor = MainAgentExecutor(
            stream_generator=streamer,
            stream_metadata={"provider": "injected_stream", "model": "test-model", "trim_max_tokens": 20_000},
            skill_catalog=SkillCatalog(()),
        )
        with patch.dict(os.environ, {"MAF_PROMPT_ENVELOPE_MODE": "string"}):
            result = await executor.execute(
                CapabilityExecutionRequest(
                    capability_id="main_agent.respond",
                    conversation_id="conv-1",
                    task_id="task-string",
                    node_id="node-string",
                    input_payload={"user_message": "string 用户问题"},
                    metadata={
                        "auto_skill_matching_enabled": False,
                        "conversation_memory": {"history_summary": "上一轮摘要"},
                    },
                    dependency_outputs={"node-a": {"response_text": "上游结果"}},
                )
            )

        prompt = prompts[0]
        self.assertIn("# 主代理稳定系统契约", prompt)
        self.assertIn("# 必需工具结果与 artifact 上下文", prompt)
        self.assertIn("# 当前用户问题", prompt)
        self.assertIn("# 最终回答前 recency guard", prompt)
        self.assertLess(prompt.index("# 对话记忆上下文"), prompt.index("# 必需工具结果与 artifact 上下文"))
        self.assertLess(prompt.index("# 必需工具结果与 artifact 上下文"), prompt.index("# 当前用户问题"))
        self.assertLess(prompt.index("# 当前用户问题"), prompt.index("# 最终回答前 recency guard"))
        for required in ("sandbox:/mnt/data", "file://", "outputs/...", "/api/v1/artifacts/", "/download"):
            self.assertIn(required, prompt)
        prompt_event = next(event for event in result.events if event.event_type == "main_agent.prompt_envelope_rendered")
        self.assertEqual(prompt_event.payload["mode"], "string")
        self.assertEqual(prompt_event.payload["effective_mode"], "string")
        self.assertLessEqual(prompt_event.payload["final_input_tokens"], prompt_event.payload["final_input_token_budget"])
        self.assertEqual(prompt_event.payload["prompt_render_metrics"]["mode"], "string")
        self.assertIn("cacheable_prefix_hash", prompt_event.payload["prompt_render_metrics"])
        self.assertIn("trim_reasons", prompt_event.payload["prompt_render_metrics"])
        self.assertNotIn("string 用户问题", str(prompt_event.payload))

    async def test_prompt_envelope_string_with_skill_match_sends_public_profile_segments(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_dir = Path(tmpdir) / "report"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                """---
name: report-writer
capability_id: skill.report_writer
display_name: 周报生成公开档案
description: 生成周报
triggers:
  - 周报
public_usage:
  overview: 公开周报用法说明。
  input_formats:
    - name: report_notes
      description: 用户提供的周报要点、日期范围和已完成工作。
  examples:
    - /report-writer 根据这些要点生成周报
parameters:
  report_notes:
    type: string
    required: true
    aliases: [周报要点, report_notes]
scripts:
  - name: internal_report
    path: scripts/internal_report.py
    runtime: python
---

# Report Writer
runtime: python_subprocess
handler: internal.ReportHandler
scripts/internal_report.py
""",
                encoding="utf-8",
            )
            catalog = SkillCatalog.from_roots([tmpdir])
            prompts: list[str] = []

            async def streamer(prompt: str):
                prompts.append(prompt)
                yield "guarded"

            executor = MainAgentExecutor(
                stream_generator=streamer,
                stream_metadata={"provider": "injected_stream", "model": "test-model", "trim_max_tokens": 2_000},
                skill_catalog=catalog,
            )

            with patch.dict(os.environ, {"MAF_PROMPT_ENVELOPE_MODE": "string"}):
                result = await executor.execute(
                    CapabilityExecutionRequest(
                        capability_id="main_agent.respond",
                        conversation_id="conv-1",
                        task_id="task-guard",
                        node_id="node-guard",
                        input_payload={"user_message": "写一个周报"},
                    )
                )

        self.assertIn("# 主代理稳定系统契约", prompts[0])
        self.assertIn("# 已选择工具公开档案", prompts[0])
        self.assertIn("# 工具输入 schema", prompts[0])
        self.assertIn("skill.report_writer", prompts[0])
        self.assertIn("公开周报用法说明", prompts[0])
        self.assertIn("report_notes", prompts[0])
        self.assertNotIn("runtime: python_subprocess", prompts[0])
        self.assertNotIn("scripts/internal_report.py", prompts[0])
        self.assertNotIn("internal.ReportHandler", prompts[0])
        prompt_event = next(event for event in result.events if event.event_type == "main_agent.prompt_envelope_rendered")
        self.assertEqual(prompt_event.payload["mode"], "string")
        self.assertEqual(prompt_event.payload["effective_mode"], "string")
        self.assertIsNone(prompt_event.payload["guard_reason"])
        self.assertIn("selected_public_tool_profiles", [segment["name"] for segment in prompt_event.payload["segments"]])
        self.assertIn("tool_input_schema", [segment["name"] for segment in prompt_event.payload["segments"]])
        self.assertNotIn("runtime: python_subprocess", str(prompt_event.payload))
        self.assertNotIn("scripts/internal_report.py", str(prompt_event.payload))

    async def test_prompt_envelope_messages_sends_native_messages_and_audits_role_fallbacks(self) -> None:
        prompts: list[object] = []

        async def streamer(prompt: object):
            prompts.append(prompt)
            yield "messages answer"

        executor = MainAgentExecutor(
            stream_generator=streamer,
            stream_metadata={
                "provider": "injected_stream",
                "model": "test-model",
                "trim_max_tokens": 20_000,
                "provider_role_capabilities": {"roles": ["system", "user"]},
                "provider_cache_capabilities": {
                    "supports_prompt_cache": True,
                    "prompt_cache_hint_enabled": True,
                    "prompt_cache_hint": {"type": "ephemeral", "scope": "cacheable_prefix"},
                },
            },
            skill_catalog=SkillCatalog(()),
        )

        with patch.dict(os.environ, {"MAF_PROMPT_ENVELOPE_MODE": "messages"}):
            result = await executor.execute(
                CapabilityExecutionRequest(
                    capability_id="main_agent.respond",
                    conversation_id="conv-1",
                    task_id="task-messages",
                    node_id="node-messages",
                    input_payload={"user_message": "messages 用户问题"},
                    metadata={
                        "auto_skill_matching_enabled": False,
                        "conversation_memory": {"history_summary": "上一轮摘要"},
                    },
                    dependency_outputs={"node-a": {"response_text": "上游工具结果"}},
                )
            )

        self.assertEqual(result.output_payload["response_text"], "messages answer")
        prompt = prompts[0]
        self.assertIsInstance(prompt, tuple)
        self.assertTrue(all(isinstance(message, LLMMessage) for message in prompt))
        self.assertEqual({message.role for message in prompt}, {"system", "user"})
        joined_user_messages = "\n".join(message.content for message in prompt if message.role == "user")
        self.assertIn("messages 用户问题", joined_user_messages)
        self.assertIn("上游工具结果", joined_user_messages)
        self.assertIn("不是用户指令", joined_user_messages)
        prompt_event = next(event for event in result.events if event.event_type == "main_agent.prompt_envelope_rendered")
        self.assertEqual(prompt_event.payload["mode"], "messages")
        self.assertEqual(prompt_event.payload["effective_mode"], "messages")
        self.assertEqual(prompt_event.payload["provider_role_capabilities"], {"roles": ["system", "user"]})
        self.assertEqual(
            prompt_event.payload["provider_cache_capabilities"],
            {
                "supports_prompt_cache": True,
                "prompt_cache_hint_enabled": True,
                "status": "enabled",
                "hint_keys": ["scope", "type"],
            },
        )
        self.assertEqual(prompt_event.payload["prompt_render_metrics"]["mode"], "messages")
        self.assertEqual(prompt_event.payload["prompt_render_metrics"]["role_fallback_count"], len(prompt_event.payload["role_fallbacks"]))
        self.assertLessEqual(prompt_event.payload["final_input_tokens"], prompt_event.payload["final_input_token_budget"])
        fallback_segments = {fallback["segment_name"]: fallback for fallback in prompt_event.payload["role_fallbacks"]}
        self.assertEqual(fallback_segments["bulk_conversation_history"]["reason"], "context_to_user_context")
        self.assertEqual(fallback_segments["required_tool_results_and_artifacts"]["reason"], "tool_to_user_context")
        self.assertNotIn("messages 用户问题", str(prompt_event.payload))
        llm_event = next(event for event in result.events if event.event_type == "main_agent.llm_call")
        self.assertEqual(llm_event.payload["prompt_envelope"]["mode"], "messages")
        self.assertEqual(llm_event.payload["prompt_envelope"]["provider_role_capabilities"], {"roles": ["system", "user"]})
        self.assertEqual(llm_event.payload["prompt_envelope"]["provider_cache_capabilities"], prompt_event.payload["provider_cache_capabilities"])
        self.assertEqual(llm_event.payload["prompt_envelope"]["role_fallbacks"], prompt_event.payload["role_fallbacks"])
        self.assertNotIn('"prompt_cache_hint":', json.dumps(llm_event.payload, ensure_ascii=False))

    async def test_executor_applies_request_level_thinking_and_reasoning_effort_separately(self) -> None:
        seen_reasoning_efforts: list[str] = []
        seen_thinking_flags: list[bool] = []

        async def streamer(prompt: str, *, reasoning_effort: str = "minimal", thinking: bool = False):
            seen_reasoning_efforts.append(reasoning_effort)
            seen_thinking_flags.append(thinking)
            yield "deep answer"

        executor = MainAgentExecutor(
            stream_generator=streamer,
            stream_metadata={
                "provider": "injected_stream",
                "model": "test-model",
                "reasoning_effort": "minimal",
            },
            skill_catalog=SkillCatalog(()),
        )

        result = await executor.execute(
            CapabilityExecutionRequest(
                capability_id="main_agent.respond",
                conversation_id="conv-1",
                task_id="task-1",
                node_id="node-1",
                input_payload={"user_message": "请深入分析"},
                metadata={"deep_thinking": True, "main_agent_reasoning_effort": "max"},
            )
        )

        self.assertEqual(seen_reasoning_efforts, ["max"])
        self.assertEqual(seen_thinking_flags, [True])
        llm_event = next(event for event in result.events if event.event_type == "main_agent.llm_call")
        self.assertEqual(llm_event.payload["reasoning_effort"], "max")
        self.assertTrue(llm_event.payload["thinking_enabled"])

    async def test_executor_forces_minimal_reasoning_when_thinking_is_disabled(self) -> None:
        seen_reasoning_efforts: list[str] = []
        seen_thinking_flags: list[bool] = []

        async def streamer(prompt: str, *, reasoning_effort: str = "minimal", thinking: bool = False):
            seen_reasoning_efforts.append(reasoning_effort)
            seen_thinking_flags.append(thinking)
            yield "answer"

        executor = MainAgentExecutor(
            stream_generator=streamer,
            stream_metadata={
                "provider": "injected_stream",
                "model": "test-model",
                "reasoning_effort": "minimal",
            },
            skill_catalog=SkillCatalog(()),
        )

        result = await executor.execute(
            CapabilityExecutionRequest(
                capability_id="main_agent.respond",
                conversation_id="conv-1",
                task_id="task-1",
                node_id="node-1",
                input_payload={"user_message": "普通回答"},
                metadata={"deep_thinking": False, "main_agent_reasoning_effort": "max"},
            )
        )

        self.assertEqual(seen_reasoning_efforts, ["minimal"])
        self.assertEqual(seen_thinking_flags, [False])
        llm_event = next(event for event in result.events if event.event_type == "main_agent.llm_call")
        self.assertEqual(llm_event.payload["reasoning_effort"], "minimal")
        self.assertFalse(llm_event.payload["thinking_enabled"])

    async def test_executor_records_safe_llm_metadata_on_provider_failure(self) -> None:
        async def broken_streamer(prompt: str):
            raise RuntimeError(
                "provider exploded api_key=secret-test-key "
                "base_url=https://example.test/v1 prompt=不要把 prompt 泄露到 audit"
            )
            yield "unreachable"

        executor = MainAgentExecutor(
            stream_generator=broken_streamer,
            stream_metadata={
                "provider": "openai_compatible",
                "model": "test-model",
                "config_source": "injected_config",
            },
            skill_catalog=SkillCatalog(()),
        )

        result = await executor.execute(
            CapabilityExecutionRequest(
                capability_id="main_agent.respond",
                conversation_id="conv-1",
                task_id="task-1",
                node_id="node-1",
                input_payload={"user_message": "不要把 prompt 泄露到 audit"},
            )
        )

        fallback_event = next(event for event in result.events if event.event_type == "main_agent.llm_stream_failed")
        self.assertEqual(result.error.code, "main_agent_llm_failed")
        self.assertEqual(fallback_event.payload["model"], "test-model")
        self.assertEqual(fallback_event.payload["error_type"], "RuntimeError")
        self.assertTrue(fallback_event.payload["partial_output_discarded"])
        self.assertNotIn("prompt", fallback_event.payload)
        self.assertNotIn("api_key", fallback_event.payload)
        self.assertNotIn("secret-test-key", str(fallback_event.payload))
        self.assertNotIn("https://example.test/v1", str(fallback_event.payload))
        self.assertNotIn("不要把 prompt 泄露到 audit", str(fallback_event.payload))

    async def test_prompt_includes_matched_skill_public_profile_without_raw_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_dir = Path(tmpdir) / "report"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                """---
name: report-writer
capability_id: skill.report_writer
display_name: 周报生成公开档案
description: 生成周报
triggers:
  - 周报
public_usage:
  overview: 公开周报格式说明。
  examples:
    - /report-writer 生成本周进展周报
---

# Report Writer
请使用项目汇报格式。
""",
                encoding="utf-8",
            )
            catalog = SkillCatalog.from_roots([tmpdir])
            prompts: list[str] = []

            async def streamer(prompt: str):
                prompts.append(prompt)
                yield "ok"

            executor = MainAgentExecutor(stream_generator=streamer, skill_catalog=catalog)
            result = await executor.execute(
                CapabilityExecutionRequest(
                    capability_id="main_agent.respond",
                    conversation_id="conv-1",
                    task_id="task-1",
                    node_id="node-1",
                    input_payload={"user_message": "写一个周报"},
                )
            )

        self.assertEqual(result.output_payload["matched_skills"], ["report-writer"])
        self.assertIn("skill.report_writer", prompts[0])
        self.assertIn("周报生成公开档案", prompts[0])
        self.assertIn("公开周报格式说明", prompts[0])
        self.assertNotIn("请使用项目汇报格式", prompts[0])

    async def test_executor_suppresses_auto_skill_matching_when_metadata_disables_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_dir = Path(tmpdir) / "report"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                """---
name: report-writer
description: 生成周报
triggers:
  - 周报
---

# Report Writer
请使用项目汇报格式。
""",
                encoding="utf-8",
            )
            catalog = SkillCatalog.from_roots([tmpdir])
            prompts: list[str] = []

            async def streamer(prompt: str):
                prompts.append(prompt)
                yield "ok"

            executor = MainAgentExecutor(stream_generator=streamer, skill_catalog=catalog)
            result = await executor.execute(
                CapabilityExecutionRequest(
                    capability_id="main_agent.respond",
                    conversation_id="conv-1",
                    task_id="task-1",
                    node_id="node-1",
                    input_payload={"user_message": "写一个周报"},
                    metadata={"auto_skill_matching_enabled": False},
                )
            )

        self.assertEqual(result.output_payload["matched_skills"], [])
        self.assertNotIn("请使用项目汇报格式", prompts[0])
        self.assertIn("skill.match_suppressed", [event.event_type for event in result.events])

    async def test_executor_keeps_forced_skill_even_when_auto_matching_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_dir = Path(tmpdir) / "report"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                """---
name: report-writer
description: 生成周报
---

# Report Writer
请使用项目汇报格式。
""",
                encoding="utf-8",
            )
            catalog = SkillCatalog.from_roots([tmpdir])

            async def streamer(_prompt: str):
                yield "ok"

            result = await MainAgentExecutor(stream_generator=streamer, skill_catalog=catalog).execute(
                CapabilityExecutionRequest(
                    capability_id="main_agent.respond",
                    conversation_id="conv-1",
                    task_id="task-1",
                    node_id="node-1",
                    input_payload={"user_message": "普通问题"},
                    metadata={"forced_skill_name": "report-writer", "auto_skill_matching_enabled": False},
                )
            )

        self.assertEqual(result.output_payload["matched_skills"], ["report-writer"])
        self.assertIn("skill.forced_selected", [event.event_type for event in result.events])

    async def test_final_response_role_prompt_prioritizes_all_upstream_results(self) -> None:
        prompts: list[str] = []

        async def streamer(prompt: str):
            prompts.append(prompt)
            yield "全局汇总"

        result = await MainAgentExecutor(stream_generator=streamer, skill_catalog=SkillCatalog(())).execute(
            CapabilityExecutionRequest(
                capability_id="main_agent.respond",
                conversation_id="conv-1",
                task_id="task-1",
                node_id="node-final",
                input_payload={"user_message": "先查品种，再做随机区组"},
                dependency_outputs={
                    "node-query": {"summary": "查到龙粳33", "row_count": 1},
                    "node-rcbd": {"summary": "RCBD设计完成", "blocks": 3},
                },
                metadata={"response_role": RESPONSE_ROLE_FINAL, "answer_scope": "task"},
            )
        )

        self.assertEqual(result.output_payload["response_text"], "全局汇总")
        self.assertEqual(result.output_payload["response_role"], RESPONSE_ROLE_FINAL)
        self.assertIn("回答角色：全局最终汇总", prompts[0])
        self.assertIn("只输出最终结论", prompts[0])
        self.assertIn("不要输出每个 skill 的中间回答", prompts[0])
        self.assertIn("不要再次调用 skill", prompts[0])
        self.assertIn("查到龙粳33", prompts[0])
        self.assertIn("RCBD设计完成", prompts[0])
        final_event = next(event for event in result.events if event.event_type == "main_agent.output_final")
        self.assertEqual(final_event.payload["response_role"], RESPONSE_ROLE_FINAL)
        transient_events = []

        async def publish_transient(event):
            transient_events.append(event)

        # Re-run with a transient collector because output deltas are no longer persisted.
        executor = MainAgentExecutor(
            stream_generator=streamer,
            skill_catalog=SkillCatalog(()),
            transient_event_publisher=publish_transient,
        )
        result = await executor.execute(
            CapabilityExecutionRequest(
                capability_id="main_agent.respond",
                conversation_id="conv-1",
                task_id="task-1b",
                node_id="node-1b",
                input_payload={"user_message": "请汇总"},
                dependency_outputs={
                    "node-a": {"response_text": "查到龙粳33", "response_role": "intermediate"},
                    "node-b": {"response_text": "RCBD设计完成", "response_role": "intermediate"},
                },
                metadata={"response_role": RESPONSE_ROLE_FINAL},
            )
        )
        delta_event = next(event for event in transient_events if event.event_type == "main_agent.output_delta")
        self.assertEqual(delta_event.payload["response_role"], RESPONSE_ROLE_FINAL)
        self.assertIn(":main_agent_response:final:", result.artifacts[0].artifact_id)

    async def test_auto_run_script_result_is_added_to_prompt(self) -> None:
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
                    print(json.dumps({"answer": "脚本处理:" + payload["query"]}, ensure_ascii=False))
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
    - answer
---

# Scripted
Use script result.
""",
                encoding="utf-8",
            )
            catalog = SkillCatalog.from_roots([tmpdir])
            prompts: list[str] = []

            async def streamer(prompt: str):
                prompts.append(prompt)
                yield "done"

            executor = MainAgentExecutor(stream_generator=streamer, skill_catalog=catalog)
            result = await executor.execute(
                CapabilityExecutionRequest(
                    capability_id="main_agent.respond",
                    conversation_id="conv-1",
                    task_id="task-1",
                    node_id="node-1",
                    input_payload={"user_message": "执行脚本"},
                )
            )

        self.assertEqual(result.output_payload["script_results"][0]["output"]["answer"], "脚本处理:执行脚本")
        self.assertIn("脚本处理:执行脚本", prompts[0])

    async def test_auto_run_script_receives_raw_skill_artifacts_not_prompt_summary(self) -> None:
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
                    artifact = payload["uploaded_artifacts"][0]
                    print(json.dumps({"answer": "content-bytes:" + str(len(artifact["content"]))}, ensure_ascii=False))
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
    - answer
---

# Scripted
Use script result.
""",
                encoding="utf-8",
            )
            catalog = SkillCatalog.from_roots([tmpdir])
            prompts: list[str] = []

            async def streamer(prompt: str):
                prompts.append(prompt)
                yield "done"

            executor = MainAgentExecutor(stream_generator=streamer, skill_catalog=catalog)
            result = await executor.execute(
                CapabilityExecutionRequest(
                    capability_id="main_agent.respond",
                    conversation_id="conv-1",
                    task_id="task-1",
                    node_id="node-1",
                    input_payload={"user_message": "执行脚本"},
                    metadata={
                        "uploaded_artifacts": [{"upload_id": "upl-1", "filename": "data.csv", "preview": {"row_count": 1}}],
                        "skill_artifacts": [{"upload_id": "upl-1", "filename": "data.csv", "content": "ped_id,design_check\nA,0\n"}],
                    },
                )
            )

        self.assertEqual(result.output_payload["script_results"][0]["output"]["answer"], "content-bytes:24")
        self.assertIn("data.csv", prompts[0])
        self.assertNotIn("ped_id,design_check", prompts[0])

    async def test_auto_run_script_receives_resolved_skill_parameters_without_raw_memory(self) -> None:
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
                    forbidden = {"conversation_memory", "memory_context", "recent_messages", "history_summary", "resolved_user_message"}
                    print(json.dumps({
                        "answer": "blocks=" + str(payload.get("blocks")),
                        "blocks": payload.get("blocks"),
                        "has_raw_memory": any(key in payload or key in metadata for key in forbidden),
                    }, ensure_ascii=False))
                    """
                ).strip(),
                encoding="utf-8",
            )
            (skill_dir / "SKILL.md").write_text(
                """---
name: scripted
triggers:
  - 生成
scripts:
  - name: answer
    path: scripts/answer.py
    auto_run: true
    inputs:
      required:
        - query
outputs:
  required:
    - answer
parameters:
  blocks:
    type: integer
    required: true
    aliases:
      - 重复
      - 区组
    patterns:
      - '(\\d+)\\s*(?:个|次)?(?:重复|区组)'
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
                    input_payload={"user_message": "按照你的操作继续生成。"},
                    metadata={
                        "conversation_memory": {
                            "current_user_message": "按照你的操作继续生成。",
                            "recent_messages": [
                                {"role": "user", "content": "你依据这份文件帮我设计一个随机区组，要求2次重复"},
                                {"role": "assistant", "content": "我理解为 blocks=999，但不能作为执行入参来源。"},
                            ],
                            "history_summary": "secret-history",
                            "resolved_user_message": "围绕要求2继续回答：按照你的操作继续生成。",
                        },
                    },
                )
            )

        output = result.output_payload["script_results"][0]["output"]
        self.assertEqual(output["blocks"], 2)
        self.assertFalse(output["has_raw_memory"])
        event_types = [event.event_type for event in result.events]
        self.assertIn("skill.input_resolved", event_types)
        resolved_event = next(event for event in result.events if event.event_type == "skill.input_resolved")
        self.assertEqual(resolved_event.payload["resolved_fields"], ["blocks"])
        self.assertNotIn("secret-history", str(resolved_event.payload))

    async def test_auto_run_script_missing_required_parameter_returns_structured_result_without_running_script(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_dir = Path(tmpdir) / "scripted"
            scripts_dir = skill_dir / "scripts"
            scripts_dir.mkdir(parents=True)
            sentinel = Path(tmpdir) / "sentinel.txt"
            (scripts_dir / "answer.py").write_text(
                textwrap.dedent(
                    f"""
                    import json
                    from pathlib import Path
                    Path({str(sentinel)!r}).write_text("ran", encoding="utf-8")
                    print(json.dumps({{"answer": "script ran"}}, ensure_ascii=False))
                    """
                ).strip(),
                encoding="utf-8",
            )
            (skill_dir / "SKILL.md").write_text(
                """---
name: scripted
triggers:
  - 生成
scripts:
  - name: answer
    path: scripts/answer.py
    auto_run: true
    inputs:
      required:
        - query
outputs:
  required:
    - answer
parameters:
  blocks:
    type: integer
    required: true
    aliases:
      - 重复
    patterns:
      - '(\\d+)\\s*(?:个|次)?重复'
---

# Scripted
""",
                encoding="utf-8",
            )
            catalog = SkillCatalog.from_roots([tmpdir])
            prompts: list[str] = []

            async def streamer(prompt: str):
                prompts.append(prompt)
                yield "done"

            result = await MainAgentExecutor(stream_generator=streamer, skill_catalog=catalog).execute(
                CapabilityExecutionRequest(
                    capability_id="main_agent.respond",
                    conversation_id="conv-1",
                    task_id="task-1",
                    node_id="node-1",
                    input_payload={"user_message": "继续生成"},
                    metadata={"conversation_memory": {"recent_messages": [{"role": "assistant", "content": "blocks=2"}]}},
                )
            )

        self.assertFalse(sentinel.exists())
        output = result.output_payload["script_results"][0]["output"]
        self.assertFalse(output["ok"])
        self.assertEqual(output["error"]["type"], "missing_input")
        self.assertEqual(output["missing"], ["blocks"])
        self.assertEqual(prompts, [])
        self.assertIsNotNone(result.interrupt)
        self.assertIn("blocks", result.interrupt.required_fields)
        event_types = [event.event_type for event in result.events]
        self.assertIn("skill.input_missing", event_types)
        self.assertNotIn("skill.script_started", event_types)

    async def test_auto_run_script_uses_llm_fallback_for_missing_scalar_parameter(self) -> None:
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
                    forbidden = {"conversation_memory", "memory_context", "recent_messages", "history_summary", "resolved_user_message"}
                    print(json.dumps({
                        "answer": "blocks=" + str(payload.get("blocks")),
                        "blocks": payload.get("blocks"),
                        "has_raw_memory": any(key in payload or key in metadata for key in forbidden),
                    }, ensure_ascii=False))
                    """
                ).strip(),
                encoding="utf-8",
            )
            (skill_dir / "SKILL.md").write_text(
                """---
name: scripted
triggers:
  - 生成
scripts:
  - name: answer
    path: scripts/answer.py
    auto_run: true
    inputs:
      required:
        - query
outputs:
  required:
    - answer
parameters:
  blocks:
    type: integer
    required: true
    aliases:
      - 重复
      - 区组
    patterns:
      - '(\\d+)\\s*(?:个|次)?(?:重复|区组)'
---

# Scripted
""",
                encoding="utf-8",
            )
            catalog = SkillCatalog.from_roots([tmpdir])
            slot_prompts: list[str] = []

            async def streamer(_prompt: str):
                yield "done"

            async def slot_generator(prompt: str) -> str:
                slot_prompts.append(prompt)
                return '{"resolved": {"blocks": {"value": 2, "source": "recent_user_message", "evidence": "不进入审计"}}}'

            result = await MainAgentExecutor(
                stream_generator=streamer,
                skill_catalog=catalog,
                skill_input_text_generator=slot_generator,
            ).execute(
                CapabilityExecutionRequest(
                    capability_id="main_agent.respond",
                    conversation_id="conv-1",
                    task_id="task-1",
                    node_id="node-1",
                    input_payload={"user_message": "按照你的操作继续生成。"},
                    metadata={
                        "conversation_memory": {
                            "current_user_message": "按照你的操作继续生成。",
                            "recent_messages": [
                                {"role": "user", "content": "重复数这个参数就是 blocks，取两次。"},
                                {"role": "assistant", "content": "我理解为 blocks=999，但不能作为执行入参来源。"},
                            ],
                            "history_summary": "secret-history",
                            "resolved_user_message": "沿用最近用户说明的 blocks 参数继续生成。",
                        },
                        "Conversation_Memory": {"history_summary": "case-secret-history"},
                    },
                )
            )

        output = result.output_payload["script_results"][0]["output"]
        self.assertEqual(output["blocks"], 2)
        self.assertFalse(output["has_raw_memory"])
        self.assertEqual(len(slot_prompts), 1)
        self.assertIn("resolved_user_message", slot_prompts[0])
        event_types = [event.event_type for event in result.events]
        self.assertIn("skill.input_resolved", event_types)
        self.assertIn("skill.script_started", event_types)
        self.assertNotIn("skill.input_missing", event_types)
        resolved_event = next(event for event in result.events if event.event_type == "skill.input_resolved")
        self.assertEqual(resolved_event.payload["sources"]["blocks"]["source"], "llm_slot_resolver:recent_user_message")
        self.assertNotIn("不进入审计", str(resolved_event.payload))
        self.assertNotIn("secret-history", str(resolved_event.payload))
        self.assertNotIn("case-secret-history", str(result.output_payload["script_results"]))

    async def test_auto_run_script_llm_failure_falls_back_to_text_and_runs_script(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_dir = Path(tmpdir) / "scripted"
            scripts_dir = skill_dir / "scripts"
            scripts_dir.mkdir(parents=True)
            sentinel = Path(tmpdir) / "llm-fallback-sentinel.txt"
            (scripts_dir / "answer.py").write_text(
                textwrap.dedent(
                    f"""
                    import json
                    import sys
                    from pathlib import Path
                    payload = json.load(sys.stdin)
                    Path({str(sentinel)!r}).write_text("ran", encoding="utf-8")
                    print(json.dumps({{"answer": "script ran", "blocks": payload.get("blocks")}}, ensure_ascii=False))
                    """
                ).strip(),
                encoding="utf-8",
            )
            (skill_dir / "SKILL.md").write_text(
                """---
name: scripted
triggers:
  - 生成
scripts:
  - name: answer
    path: scripts/answer.py
    auto_run: true
    inputs:
      required:
        - query
outputs:
  required:
    - answer
parameters:
  blocks:
    type: integer
    required: true
---

# Scripted
""",
                encoding="utf-8",
            )
            catalog = SkillCatalog.from_roots([tmpdir])

            async def streamer(_prompt: str):
                yield "done"

            async def slot_generator(_prompt: str) -> str:
                return "not-json"

            result = await MainAgentExecutor(
                stream_generator=streamer,
                skill_catalog=catalog,
                skill_input_text_generator=slot_generator,
            ).execute(
                CapabilityExecutionRequest(
                    capability_id="main_agent.respond",
                    conversation_id="conv-1",
                    task_id="task-1",
                    node_id="node-1",
                    input_payload={"user_message": "继续生成"},
                    metadata={"conversation_memory": {"recent_messages": [{"role": "user", "content": "blocks 是两个。"}]}},
                )
            )

            self.assertTrue(sentinel.exists())
            output = result.output_payload["script_results"][0]["output"]
            self.assertEqual(output["blocks"], 2)
            self.assertIsNone(result.interrupt)
            event_types = [event.event_type for event in result.events]
            self.assertIn("skill.input_resolved", event_types)
            self.assertIn("skill.input_resolution_diagnostic", event_types)
            self.assertIn("skill.script_started", event_types)
            self.assertNotIn("skill.input_missing", event_types)
            diagnostic_event = next(event for event in result.events if event.event_type == "skill.input_resolution_diagnostic")
            self.assertEqual(diagnostic_event.payload["diagnostics"], ["llm_invalid_json"])
            resolved_event = next(event for event in result.events if event.event_type == "skill.input_resolved")
            self.assertEqual(resolved_event.payload["sources"]["blocks"]["source"], "recent_user_message")

    async def test_auto_run_script_missing_required_artifact_parameter_does_not_run_script(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_dir = Path(tmpdir) / "scripted"
            scripts_dir = skill_dir / "scripts"
            scripts_dir.mkdir(parents=True)
            sentinel = Path(tmpdir) / "artifact-sentinel.txt"
            (scripts_dir / "answer.py").write_text(
                textwrap.dedent(
                    f"""
                    import json
                    from pathlib import Path
                    Path({str(sentinel)!r}).write_text("ran", encoding="utf-8")
                    print(json.dumps({{"answer": "script ran"}}, ensure_ascii=False))
                    """
                ).strip(),
                encoding="utf-8",
            )
            (skill_dir / "SKILL.md").write_text(
                """---
name: scripted
triggers:
  - 随机区组
scripts:
  - name: answer
    path: scripts/answer.py
    auto_run: true
    inputs:
      required:
        - query
outputs:
  required:
    - answer
parameters:
  blocks:
    type: integer
    required: true
    patterns:
      - '(\\d+)\\s*(?:个|次)?重复'
  material_data:
    type: artifact
    required: true
    source: artifact
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
                    input_payload={"user_message": "请生成随机区组，要求2次重复"},
                )
            )

        self.assertFalse(sentinel.exists())
        output = result.output_payload["script_results"][0]["output"]
        self.assertEqual(output["missing"], ["material_data"])
        self.assertIsNotNone(result.interrupt)
        self.assertIn("material_data", result.interrupt.required_fields)
        self.assertIs(result.interrupt.required_fields["material_data"].get("accepts_upload"), True)
        event_types = [event.event_type for event in result.events]
        self.assertIn("skill.input_missing", event_types)
        self.assertNotIn("skill.script_started", event_types)

    async def test_auto_run_script_artifact_parameter_success_runs_script_with_marker_and_raw_artifact(self) -> None:
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
                    print(json.dumps({
                        "answer": "ok",
                        "blocks": payload.get("blocks"),
                        "material_data": payload.get("material_data"),
                        "artifact_content": payload["uploaded_artifacts"][0].get("content"),
                    }, ensure_ascii=False))
                    """
                ).strip(),
                encoding="utf-8",
            )
            (skill_dir / "SKILL.md").write_text(
                """---
name: scripted
triggers:
  - 随机区组
scripts:
  - name: answer
    path: scripts/answer.py
    auto_run: true
    inputs:
      required:
        - query
outputs:
  required:
    - answer
parameters:
  blocks:
    type: integer
    required: true
    patterns:
      - '(\\d+)\\s*(?:个|次)?重复'
  material_data:
    type: artifact
    required: true
    source: artifact
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
                    input_payload={"user_message": "请生成随机区组，要求2次重复"},
                    metadata={
                        "skill_artifacts": [
                            {
                                "upload_id": "upl-1",
                                "filename": "materials.csv",
                                "content": "ped_id,design_check\nA,0\n",
                            }
                        ]
                    },
                )
            )

        output = result.output_payload["script_results"][0]["output"]
        self.assertEqual(output["blocks"], 2)
        self.assertEqual(output["material_data"], {"available": True, "count": 1})
        self.assertEqual(output["artifact_content"], "ped_id,design_check\nA,0\n")
        event_types = [event.event_type for event in result.events]
        self.assertIn("skill.input_resolved", event_types)
        self.assertIn("skill.script_started", event_types)
        self.assertNotIn("skill.input_missing", event_types)

    async def test_auto_run_script_input_contract_missing_returns_structured_result_before_started_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_dir = Path(tmpdir) / "scripted"
            scripts_dir = skill_dir / "scripts"
            scripts_dir.mkdir(parents=True)
            sentinel = Path(tmpdir) / "contract-sentinel.txt"
            (scripts_dir / "answer.py").write_text(
                textwrap.dedent(
                    f"""
                    import json
                    from pathlib import Path
                    Path({str(sentinel)!r}).write_text("ran", encoding="utf-8")
                    print(json.dumps({{"answer": "script ran"}}, ensure_ascii=False))
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
    inputs:
      required:
        - query
        - user_id
outputs:
  required:
    - answer
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
                )
            )

        self.assertFalse(sentinel.exists())
        output = result.output_payload["script_results"][0]["output"]
        self.assertEqual(output["missing"], ["user_id"])
        self.assertIsNotNone(result.interrupt)
        self.assertIn("user_id", result.interrupt.required_fields)
        event_types = [event.event_type for event in result.events]
        self.assertIn("skill.input_missing", event_types)
        self.assertNotIn("skill.script_started", event_types)

    async def test_forced_skill_is_used_even_without_trigger_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_dir = Path(tmpdir) / "rcbd"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                """---
name: mini-breedstat-rcbd
capability_id: skill.mini_breedstat_rcbd
display_name: RCBD 公开 Skill
description: 生成 RCBD 随机区组设计
triggers:
  - 完全不会出现在问题里的触发词
public_usage:
  overview: 公开说明：用于生成随机区组设计。
  examples:
    - /mini-breedstat-rcbd 上传材料表并生成 RCBD
---

# RCBD
请使用随机区组设计 Skill。
""",
                encoding="utf-8",
            )
            catalog = SkillCatalog.from_roots([tmpdir])
            prompts: list[str] = []

            async def streamer(prompt: str):
                prompts.append(prompt)
                yield "done"

            result = await MainAgentExecutor(stream_generator=streamer, skill_catalog=catalog).execute(
                CapabilityExecutionRequest(
                    capability_id="main_agent.respond",
                    conversation_id="conv-1",
                    task_id="task-1",
                    node_id="node-1",
                    input_payload={"user_message": "请帮我处理这个材料表"},
                    metadata={
                        "forced_skill_capability_id": "skill.mini_breedstat_rcbd",
                        "forced_skill_name": "mini-breedstat-rcbd",
                    },
                )
            )

        self.assertEqual(result.output_payload["matched_skills"], ["mini-breedstat-rcbd"])
        self.assertIn("skill.mini_breedstat_rcbd", prompts[0])
        self.assertIn("RCBD 公开 Skill", prompts[0])
        self.assertIn("公开说明：用于生成随机区组设计", prompts[0])
        self.assertNotIn("请使用随机区组设计 Skill", prompts[0])
        event_types = [event.event_type for event in result.events]
        self.assertIn("skill.forced_selected", event_types)
        self.assertNotIn("skill.match_fallback", event_types)

    async def test_input_payload_cannot_force_skill_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_dir = Path(tmpdir) / "rcbd"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                """---
name: mini-breedstat-rcbd
description: 生成 RCBD 随机区组设计
triggers:
  - 完全不会出现在问题里的触发词
---

# RCBD
请使用随机区组设计 Skill。
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
                    input_payload={
                        "user_message": "普通问题",
                        "forced_skill_capability_id": "skill.mini_breedstat_rcbd",
                        "forced_skill_name": "mini-breedstat-rcbd",
                        "forced_skill_source": "planner",
                    },
                )
            )

        self.assertEqual(result.output_payload["matched_skills"], [])
        event_types = [event.event_type for event in result.events]
        self.assertNotIn("skill.forced_selected", event_types)
        self.assertIn("skill.match_fallback", event_types)

    async def test_delegated_main_agent_skill_does_not_run_auto_scripts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            skill_dir = root / "delegated"
            scripts_dir = skill_dir / "scripts"
            scripts_dir.mkdir(parents=True)
            (scripts_dir / "should_not_run.py").write_text(
                'raise SystemExit("should not run")',
                encoding="utf-8",
            )
            (skill_dir / "SKILL.md").write_text(
                textwrap.dedent(
                    """---
name: delegated-skill
capability_id: skill.delegated_skill
display_name: Delegated Skill
description: 委托技能
triggers:
  - 委托技能
public_usage:
  overview: 公开说明：委托主代理回答，不直接运行脚本。
  examples:
    - /delegated-skill 说明委托技能如何使用
scripts:
  - name: should-not-run
    path: scripts/should_not_run.py
    runtime: python
    auto_run: true
execution:
  mode: delegated_main_agent
---

# Delegated Skill
只注入指令，不执行脚本。
"""
                ).strip(),
                encoding="utf-8",
            )
            catalog = SkillCatalog.from_roots([root])
            seen_prompts: list[str] = []

            async def streamer(prompt: str):
                seen_prompts.append(prompt)
                yield "done"

            result = await MainAgentExecutor(stream_generator=streamer, skill_catalog=catalog).execute(
                CapabilityExecutionRequest(
                    capability_id="main_agent.respond",
                    conversation_id="conv-1",
                    task_id="task-1",
                    node_id="node-1",
                    input_payload={"user_message": "请使用委托技能"},
                )
            )

        self.assertEqual(result.output_payload["response_text"], "done")
        self.assertEqual(result.output_payload["script_results"], [])
        event_types = [event.event_type for event in result.events]
        self.assertNotIn("skill.script_started", event_types)
        self.assertNotIn("skill.script_completed", event_types)
        self.assertIn("Delegated Skill", seen_prompts[0])
        self.assertIn("公开说明：委托主代理回答", seen_prompts[0])
        self.assertNotIn("scripts/should_not_run.py", seen_prompts[0])
        self.assertNotIn("Skill 脚本输出", seen_prompts[0])


if __name__ == "__main__":
    unittest.main()
