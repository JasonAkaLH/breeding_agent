from __future__ import annotations

import json
import textwrap

from src.core.enums import EventVisibility

from tests.api.support import APITestCase


class SkillInputResolutionRuntimeAPITest(APITestCase):
    async def test_followup_skill_script_receives_structured_parameter_without_raw_memory(self) -> None:
        skill_dir = self.workspace / "skills" / "scripted"
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
name: scripted-rcbd
triggers:
  - 随机区组
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

# Scripted RCBD
""",
            encoding="utf-8",
        )

        answer_prompts: list[str] = []

        async def streamer(prompt: str):
            answer_prompts.append(prompt)
            yield "已完成。"

        await self.reconfigure_runtime(
            main_agent_stream_generator=streamer,
            skill_roots=[self.workspace / "skills"],
        )

        first = await self.submit_message(
            conversation_id="conv-skill-input-resolution",
            content="你依据这份文件帮我设计一个随机区组，要求2次重复",
            capability_id="main_agent.respond",
        )
        self.assertEqual(first.status_code, 202)
        await self.wait_for_terminal_task(first.json()["task_id"])

        second = await self.submit_message(
            conversation_id="conv-skill-input-resolution",
            content="按照你的操作继续生成。",
            capability_id="main_agent.respond",
        )
        self.assertEqual(second.status_code, 202)
        second_task_id = second.json()["task_id"]
        await self.wait_for_terminal_task(second_task_id)

        self.assertGreaterEqual(len(answer_prompts), 2)
        self.assertIn('"blocks": 2', answer_prompts[-1])
        self.assertIn('"has_raw_memory": false', answer_prompts[-1])

        events = await self.runtime.storage.list_events_for_task(second_task_id)
        resolved_event = next(event for event in events if event.event_type == "skill.input_resolved")
        self.assertEqual(resolved_event.visibility, EventVisibility.AUDIT_ONLY)
        self.assertEqual(resolved_event.payload["resolved_fields"], ["blocks"])
        self.assertEqual(resolved_event.payload["sources"]["blocks"]["source"], "recent_user_message")
        self.assertNotIn("要求2次重复", str(resolved_event.payload))

    async def test_runtime_uses_main_agent_llm_for_missing_skill_scalar_without_raw_memory_leak(self) -> None:
        skill_dir = self.workspace / "skills" / "scripted-llm"
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
name: scripted-rcbd-llm
triggers:
  - 随机区组
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

# Scripted RCBD LLM
""",
            encoding="utf-8",
        )

        answer_prompts: list[str] = []
        slot_prompts: list[str] = []

        async def streamer(prompt: str):
            answer_prompts.append(prompt)
            yield "已完成。"

        class FakeLLMClient:
            def __init__(self, **_kwargs) -> None:
                pass

            def safe_metadata(self, *, config_source: str | None = None, reasoning_effort: str | None = None) -> dict:
                return {"provider": "fake", "config_source": config_source, "reasoning_effort": reasoning_effort}

            async def generate_text(self, prompt: str, *, thinking: bool = False, reasoning_effort: str = "minimal") -> str:
                slot_prompts.append(prompt)
                self_test_payload = {
                    "resolved": {
                        "blocks": {
                            "value": 2,
                            "source": "recent_user_message",
                            "evidence": "不应出现在审计",
                        }
                    }
                }
                return json.dumps(self_test_payload, ensure_ascii=False)

        await self.reconfigure_runtime(
            main_agent_stream_generator=streamer,
            main_agent_llm_client_factory=FakeLLMClient,
            skill_roots=[self.workspace / "skills"],
        )

        first = await self.submit_message(
            conversation_id="conv-skill-input-llm",
            content="补充设置：重复数这个参数就是 blocks，取两次。",
            capability_id="main_agent.respond",
        )
        self.assertEqual(first.status_code, 202)
        await self.wait_for_terminal_task(first.json()["task_id"])

        second = await self.submit_message(
            conversation_id="conv-skill-input-llm",
            content="按照你的操作继续生成随机区组。",
            capability_id="main_agent.respond",
        )
        self.assertEqual(second.status_code, 202)
        second_task_id = second.json()["task_id"]
        await self.wait_for_terminal_task(second_task_id)

        self.assertEqual(len(slot_prompts), 1)
        self.assertGreaterEqual(len(answer_prompts), 2)
        self.assertIn('"blocks": 2', answer_prompts[-1])
        self.assertIn('"has_raw_memory": false', answer_prompts[-1])

        events = await self.runtime.storage.list_events_for_task(second_task_id)
        resolved_event = next(event for event in events if event.event_type == "skill.input_resolved")
        self.assertEqual(resolved_event.visibility, EventVisibility.AUDIT_ONLY)
        self.assertEqual(resolved_event.payload["sources"]["blocks"]["source"], "llm_slot_resolver:recent_user_message")
        self.assertNotIn("不应出现在审计", str(resolved_event.payload))
        self.assertNotIn("重复数这个参数", str(resolved_event.payload))
