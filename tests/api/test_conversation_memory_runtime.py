from __future__ import annotations

import json

from src.core.enums import EventVisibility

from tests.api.support import APITestCase


class ConversationMemoryRuntimeAPITest(APITestCase):
    async def test_memory_is_built_before_planning_and_audit_event_is_safe(self) -> None:
        planner_prompts: list[str] = []
        answer_prompts: list[str] = []

        async def planner(prompt: str) -> str:
            planner_prompts.append(prompt)
            return json.dumps(
                {
                    "nodes": [
                        {
                            "node_id": "answer",
                            "capability_id": "main_agent.respond",
                        }
                    ]
                },
                ensure_ascii=False,
            )

        async def streamer(prompt: str):
            answer_prompts.append(prompt)
            yield "已完成。"

        await self.reconfigure_runtime(
            planner_text_generator=planner,
            main_agent_stream_generator=streamer,
            skill_roots=[],
        )

        first = await self.submit_message(
            conversation_id="conv-memory-runtime",
            content="查一下龙粳33的品种信息",
            capability_id="main_agent.respond",
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

        self.assertEqual(len(planner_prompts), 1)
        self.assertIn("当前用户原文", planner_prompts[0])
        self.assertIn("那它的基因型数据库里有什么", planner_prompts[0])
        self.assertIn("系统根据历史补全后的 effective question", planner_prompts[0])
        self.assertIn("龙粳33", planner_prompts[0])
        self.assertIn("基因型", planner_prompts[0])

        self.assertGreaterEqual(len(answer_prompts), 2)
        self.assertIn("对话记忆上下文", answer_prompts[-1])
        self.assertIn("查一下龙粳33的品种信息", answer_prompts[-1])
        self.assertIn("系统根据历史补全后的 effective question", answer_prompts[-1])

        events = await self.runtime.storage.list_events_for_task(second_task_id)
        event_types = [event.event_type for event in events]
        self.assertLess(event_types.index("conversation.memory_built"), event_types.index("workflow.plan_built"))
        memory_event = next(event for event in events if event.event_type == "conversation.memory_built")
        self.assertEqual(memory_event.visibility, EventVisibility.AUDIT_ONLY)
        self.assertTrue(memory_event.payload["resolved"])
        self.assertNotIn("recent_messages", memory_event.payload)
        self.assertNotIn("history_summary", memory_event.payload)

    async def test_memory_builder_failure_falls_back_without_failing_task(self) -> None:
        class BrokenMemoryBuilder:
            async def build(self, _request, *, account_id=None):
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
            capability_id="main_agent.respond",
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
