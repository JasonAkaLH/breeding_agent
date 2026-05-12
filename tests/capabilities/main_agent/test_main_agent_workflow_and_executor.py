from __future__ import annotations

import asyncio
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from src.capabilities.main_agent import MainAgentExecutor, MainAgentWorkflowProvider
from src.core.contracts import CapabilityExecutionRequest
from src.orchestration.models import OrchestrationRequest
from src.integrations.codex_skills import SkillCatalog


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

    async def test_executor_streams_answer_chunks_and_returns_text_artifact(self) -> None:
        seen_prompts: list[str] = []

        async def streamer(prompt: str):
            seen_prompts.append(prompt)
            yield "hello"
            yield " world"

        executor = MainAgentExecutor(stream_generator=streamer, skill_catalog=SkillCatalog(()))
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
        self.assertEqual(
            [event.event_type for event in result.events if event.event_type == "main_agent.output_delta"],
            ["main_agent.output_delta", "main_agent.output_delta"],
        )

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
                    "task-1:query_data:sql_execute_readonly": {
                        "columns": ["variety_name", "approval_num"],
                        "rows": [{"variety_name": "龙粳18", "approval_num": "黑审稻"}],
                        "route_id": "approval_variety_db",
                        "row_count": 1,
                        "preview_row_count": 1,
                        "truncated": False,
                    }
                },
            )
        )

        self.assertEqual(result.output_payload["response_text"], "这是整理后的答案")
        self.assertIn("上游能力结果上下文", seen_prompts[0])
        self.assertIn("approval_variety_db", seen_prompts[0])
        self.assertIn("columns", seen_prompts[0])
        self.assertIn("rows", seen_prompts[0])
        self.assertIn("龙粳18", seen_prompts[0])

    async def test_executor_streams_reasoning_content_as_separate_frontend_events(self) -> None:
        async def streamer(prompt: str, *, reasoning_effort: str = "minimal", thinking: bool = False):
            yield {"reasoning": "先分析问题。", "answer": None}
            yield {"answer": "最终回答", "reasoning": None}

        executor = MainAgentExecutor(stream_generator=streamer, skill_catalog=SkillCatalog(()))
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

        reasoning_events = [event for event in result.events if event.event_type == "main_agent.reasoning_delta"]
        answer_events = [event for event in result.events if event.event_type == "main_agent.output_delta"]

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
            executor = MainAgentExecutor(skill_catalog=SkillCatalog(()))
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

        reasoning_events = [event for event in result.events if event.event_type == "main_agent.reasoning_delta"]
        self.assertEqual([event.payload["delta"] for event in reasoning_events], ["默认绑定思考"])
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
                metadata={"deep_thinking": True, "main_agent_reasoning_effort": "medium"},
            )
        )

        self.assertEqual(seen_reasoning_efforts, ["medium"])
        self.assertEqual(seen_thinking_flags, [True])
        llm_event = next(event for event in result.events if event.event_type == "main_agent.llm_call")
        self.assertEqual(llm_event.payload["reasoning_effort"], "medium")
        self.assertTrue(llm_event.payload["thinking_enabled"])

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

        fallback_event = next(event for event in result.events if event.event_type == "main_agent.llm_fallback")
        self.assertEqual(result.error.code, "main_agent_llm_failed")
        self.assertEqual(fallback_event.payload["model"], "test-model")
        self.assertEqual(fallback_event.payload["diagnostic"], "RuntimeError")
        self.assertNotIn("prompt", fallback_event.payload)
        self.assertNotIn("api_key", fallback_event.payload)
        self.assertNotIn("secret-test-key", str(fallback_event.payload))
        self.assertNotIn("https://example.test/v1", str(fallback_event.payload))
        self.assertNotIn("不要把 prompt 泄露到 audit", str(fallback_event.payload))

    async def test_prompt_includes_matched_skill_body(self) -> None:
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
                )
            )

        self.assertEqual(result.output_payload["matched_skills"], ["report-writer"])
        self.assertIn("请使用项目汇报格式", prompts[0])

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
        self.assertIn("blocks", prompts[0])
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

    async def test_auto_run_script_llm_fallback_failure_keeps_structured_missing_without_running_script(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_dir = Path(tmpdir) / "scripted"
            scripts_dir = skill_dir / "scripts"
            scripts_dir.mkdir(parents=True)
            sentinel = Path(tmpdir) / "llm-fallback-sentinel.txt"
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

        self.assertFalse(sentinel.exists())
        output = result.output_payload["script_results"][0]["output"]
        self.assertEqual(output["missing"], ["blocks"])
        event_types = [event.event_type for event in result.events]
        self.assertIn("skill.input_missing", event_types)
        self.assertIn("skill.input_resolution_diagnostic", event_types)
        self.assertNotIn("skill.script_started", event_types)
        diagnostic_event = next(event for event in result.events if event.event_type == "skill.input_resolution_diagnostic")
        self.assertEqual(diagnostic_event.payload["diagnostics"], ["llm_invalid_json"])

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
        self.assertIn("请使用随机区组设计 Skill", prompts[0])
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
description: 委托技能
triggers:
  - 委托技能
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
        self.assertNotIn("Skill 脚本输出", seen_prompts[0])


if __name__ == "__main__":
    unittest.main()
