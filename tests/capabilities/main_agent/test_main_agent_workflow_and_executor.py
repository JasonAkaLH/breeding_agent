from __future__ import annotations

import asyncio
import tempfile
import textwrap
import unittest
from pathlib import Path

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
        self.assertIn("a.txt", seen_prompts[0])
        self.assertNotIn("secret", seen_prompts[0])
        self.assertEqual(
            [event.event_type for event in result.events if event.event_type == "main_agent.output_delta"],
            ["main_agent.output_delta", "main_agent.output_delta"],
        )

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


if __name__ == "__main__":
    unittest.main()
