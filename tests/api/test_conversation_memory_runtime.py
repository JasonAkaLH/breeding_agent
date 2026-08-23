from __future__ import annotations

import json
from unittest.mock import patch

from src.core.enums import EventVisibility

from tests.api.support import APITestCase


class ConversationMemoryRuntimeAPITest(APITestCase):
    async def test_memory_is_built_before_agent_sampling_and_audit_event_is_safe(self) -> None:
        answer_prompts: list[str] = []

        async def streamer(prompt: str):
            answer_prompts.append(prompt)
            yield "已完成。"

        await self.reconfigure_runtime(
            main_agent_stream_generator=streamer,
            skill_roots=[],
        )

        first = await self.submit_message(
            conversation_id="conv-memory-runtime",
            content="查一下龙粳33的品种信息",
            capability_id=None,
        )
        self.assertEqual(first.status_code, 202)
        first_terminal = await self.wait_for_terminal_task(first.json()["task_id"])
        self.assertEqual(first_terminal["status"], "completed")

        second = await self.submit_message(
            conversation_id="conv-memory-runtime",
            content="那它的基因型数据库里有什么？",
            capability_id=None,
        )
        self.assertEqual(second.status_code, 202)
        second_task_id = second.json()["task_id"]
        second_terminal = await self.wait_for_terminal_task(second_task_id)
        self.assertEqual(second_terminal["status"], "completed")

        self.assertGreaterEqual(len(answer_prompts), 2)
        self.assertIn("conversation_memory", answer_prompts[-1])
        self.assertIn("查一下龙粳33的品种信息", answer_prompts[-1])
        self.assertIn("resolved_user_message", answer_prompts[-1])
        self.assertIn("那它的基因型数据库里有什么", answer_prompts[-1])
        self.assertIn("龙粳33", answer_prompts[-1])

        events = await self.runtime.storage.list_events_for_task(second_task_id)
        event_types = [event.event_type for event in events]
        self.assertLess(event_types.index("conversation.memory_built"), event_types.index("agent.run.started"))
        memory_event = next(event for event in events if event.event_type == "conversation.memory_built")
        self.assertEqual(memory_event.visibility, EventVisibility.AUDIT_ONLY)
        self.assertTrue(memory_event.payload["resolved"])
        self.assertNotIn("recent_messages", memory_event.payload)
        self.assertNotIn("history_summary", memory_event.payload)

    async def test_injected_llm_resolution_generator_builds_effective_question_before_agent_sampling(self) -> None:
        resolution_prompts: list[str] = []
        resolution_profiles: list[dict | None] = []
        answer_prompts: list[str] = []

        async def resolver(prompt: str, **kwargs) -> str:
            resolution_prompts.append(prompt)
            resolution_profiles.append(kwargs.get("prompt_profile"))
            if "那它的基因型" not in prompt:
                return json.dumps(
                    {
                        "should_resolve": False,
                        "resolved_user_message": None,
                        "referenced_entity": None,
                        "entity_type": None,
                        "source": {"type": None, "message_id": None, "evidence_text": None},
                        "confidence": "high",
                        "reason": "当前问题不需要补全。",
                        "risk_flags": [],
                    },
                    ensure_ascii=False,
                )
            return json.dumps(
                {
                    "should_resolve": True,
                    "resolved_user_message": "查询龙粳18的基因型信息",
                    "referenced_entity": "龙粳18",
                    "entity_type": "crop_variety",
                    "source": {
                        "type": "recent_message",
                        "message_id": None,
                        "evidence_text": "再查一下龙粳18",
                    },
                    "confidence": "high",
                    "reason": "多个候选实体按最近明确提到的业务实体解析。",
                    "risk_flags": ["multiple_candidate_entities_resolved_by_recency"],
                },
                ensure_ascii=False,
            )

        async def streamer(prompt: str):
            answer_prompts.append(prompt)
            yield "已完成。"

        await self.reconfigure_runtime(
            main_agent_stream_generator=streamer,
            conversation_memory_resolution_generator=resolver,
            skill_roots=[],
        )

        with patch.dict("os.environ", {"MAF_PROMPT_ENVELOPE_MODE": "string"}):
            for content in ("查一下龙粳33的品种信息", "再查一下龙粳18"):
                response = await self.submit_message(
                    conversation_id="conv-memory-llm-resolution",
                    content=content,
                    capability_id=None,
                )
                self.assertEqual(response.status_code, 202)
                terminal = await self.wait_for_terminal_task(response.json()["task_id"])
                self.assertEqual(terminal["status"], "completed")
                handle = self.runtime._running_tasks.get(response.json()["task_id"])
                if handle is not None:
                    await handle

            response = await self.submit_message(
                conversation_id="conv-memory-llm-resolution",
                content="那它的基因型数据库里有什么？",
                capability_id=None,
            )
            self.assertEqual(response.status_code, 202)
            task_id = response.json()["task_id"]
            terminal = await self.wait_for_terminal_task(task_id)
            self.assertEqual(terminal["status"], "completed")
            handle = self.runtime._running_tasks.get(task_id)
            if handle is not None:
                await handle

        self.assertEqual(len(resolution_prompts), 3)
        self.assertEqual(resolution_profiles[-1]["template_id"], "conversation_memory_resolution")
        self.assertIn("默认选择最近一次被明确提到的业务实体", resolution_prompts[-1])
        self.assertIn("龙粳33", resolution_prompts[-1])
        self.assertIn("龙粳18", resolution_prompts[-1])
        self.assertIn("resolved_user_message", answer_prompts[-1])
        self.assertIn("查询龙粳18的基因型信息", answer_prompts[-1])

        memory_event = next(
            event
            for event in await self.runtime.storage.list_events_for_task(task_id)
            if event.event_type == "conversation.memory_built"
        )
        self.assertTrue(memory_event.payload["resolved"])
        self.assertEqual(memory_event.payload["resolution_prompt_profile"]["template_id"], "conversation_memory_resolution")

    async def test_memory_builder_failure_falls_back_without_failing_task(self) -> None:
        class BrokenMemoryBuilder:
            async def build(self, _request, *, username=None):
                raise RuntimeError("memory unavailable")

        async def streamer(_prompt: str):
            yield "ok"

        await self.reconfigure_runtime(
            conversation_memory_builder=BrokenMemoryBuilder(),
            main_agent_stream_generator=streamer,
            skill_roots=[],
        )

        response = await self.submit_message(
            conversation_id="conv-memory-fallback",
            content="你好",
            capability_id=None,
        )
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]
        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")

        events = await self.runtime.storage.list_events_for_task(task_id)
        fallback_event = next(event for event in events if event.event_type == "conversation.memory_fallback")
        self.assertEqual(fallback_event.visibility, EventVisibility.AUDIT_ONLY)
        self.assertEqual(fallback_event.payload["fallback_reason"], "memory_builder_failed")
        self.assertEqual(fallback_event.payload["error_type"], "RuntimeError")
