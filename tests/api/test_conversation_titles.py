from __future__ import annotations

from tests.api.support import APITestCase


class ConversationTitleAPITest(APITestCase):
    async def test_new_conversation_generates_title_with_minimal_non_thinking_llm(self) -> None:
        calls: list[dict[str, object]] = []

        class RecordingTitleClient:
            async def generate_text(self, prompt: str, *, thinking: bool = False, reasoning_effort: str = "high") -> str:
                calls.append({"prompt": prompt, "thinking": thinking, "reasoning_effort": reasoning_effort})
                return "龙粳33品种查询"

        await self.reconfigure_runtime(
            main_agent_llm_client_factory=lambda **_kwargs: RecordingTitleClient(),
            main_agent_stream_generator=lambda _prompt: ["已收到。"],
            enable_conversation_title_llm=True,
        )

        response = await self.submit_message(
            conversation_id="conv-title",
            content="帮我查询龙粳33的品种信息",
            capability_id=None,
        )
        self.assertEqual(response.status_code, 202)

        async def title_generated() -> bool:
            conversation = await self.runtime.storage.get_conversation("conv-title")
            return conversation is not None and conversation.title == "龙粳33品种查询"

        await self.wait_for_condition(title_generated)
        self.assertEqual(len(calls), 1)
        self.assertIn("帮我查询龙粳33的品种信息", str(calls[0]["prompt"]))
        self.assertIs(calls[0]["thinking"], False)
        self.assertEqual(calls[0]["reasoning_effort"], "minimal")

    async def test_title_generation_failure_keeps_title_empty_so_next_round_retries_with_all_user_messages(self) -> None:
        calls: list[str] = []

        async def retrying_title_generator(message: str) -> str:
            calls.append(message)
            if len(calls) == 1:
                raise RuntimeError("title provider unavailable")
            return "两轮问题总结"

        await self.reconfigure_runtime(
            conversation_title_generator=retrying_title_generator,
            main_agent_stream_generator=lambda _prompt: ["已收到。"],
        )

        response = await self.submit_message(
            conversation_id="conv-title-fallback",
            content="这是一个需要被概括的很长很长的问题内容",
            capability_id=None,
        )
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]
        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")

        async def first_title_attempt_finished() -> bool:
            return len(calls) == 1

        await self.wait_for_condition(first_title_attempt_finished)

        conversation = await self.runtime.storage.get_conversation("conv-title-fallback")
        self.assertIsNotNone(conversation)
        self.assertIsNone(conversation.title)
        self.assertEqual(len(calls), 1)

        second = await self.submit_message(
            conversation_id="conv-title-fallback",
            content="第二轮补充问题",
            capability_id=None,
        )
        self.assertEqual(second.status_code, 202)

        async def title_generated() -> bool:
            conversation = await self.runtime.storage.get_conversation("conv-title-fallback")
            return conversation is not None and conversation.title == "两轮问题总结"

        await self.wait_for_condition(title_generated)
        self.assertEqual(len(calls), 2)
        self.assertIn("用户第1轮：这是一个需要被概括的很长很长的问题内容", calls[1])
        self.assertIn("用户第2轮：第二轮补充问题", calls[1])

    async def test_owner_can_rename_conversation_and_list_sees_new_title(self) -> None:
        await self.reconfigure_runtime(main_agent_stream_generator=lambda _prompt: ["已收到。"])
        response = await self.submit_message(conversation_id="conv-rename", content="你好", capability_id=None)
        self.assertEqual(response.status_code, 202)

        renamed = await self.client.patch("/api/v1/conversations", json={"conversation_id": "conv-rename", "title": "业务问答复盘"})

        self.assertEqual(renamed.status_code, 200)
        self.assertEqual(renamed.json()["title"], "业务问答复盘")
        conversations = await self.client.get("/api/v1/conversations")
        self.assertEqual(conversations.status_code, 200)
        self.assertIn("业务问答复盘", [item["title"] for item in conversations.json()["conversations"]])

    async def test_follow_up_message_does_not_regenerate_or_overwrite_title(self) -> None:
        calls: list[str] = []

        async def title_generator(message: str) -> str:
            calls.append(message)
            return "首轮自动标题"

        await self.reconfigure_runtime(
            conversation_title_generator=title_generator,
            main_agent_stream_generator=lambda _prompt: ["已收到。"],
        )
        first = await self.submit_message(conversation_id="conv-follow-title", content="第一轮问题", capability_id=None)
        self.assertEqual(first.status_code, 202)
        await self.wait_for_terminal_task(first.json()["task_id"])

        async def title_generated() -> bool:
            conversation = await self.runtime.storage.get_conversation("conv-follow-title")
            return conversation is not None and conversation.title == "首轮自动标题"

        await self.wait_for_condition(title_generated)
        renamed = await self.client.patch("/api/v1/conversations", json={"conversation_id": "conv-follow-title", "title": "用户手动标题"})
        self.assertEqual(renamed.status_code, 200)

        second = await self.submit_message(conversation_id="conv-follow-title", content="第二轮问题", capability_id=None)
        self.assertEqual(second.status_code, 202)
        conversation = await self.runtime.storage.get_conversation("conv-follow-title")
        self.assertIsNotNone(conversation)
        self.assertEqual(conversation.title, "用户手动标题")
        self.assertEqual(calls, ["用户第1轮：第一轮问题"])

    async def test_rename_is_owner_scoped_and_validates_title(self) -> None:
        await self.reconfigure_runtime(main_agent_stream_generator=lambda _prompt: ["已收到。"])
        response = await self.submit_message(conversation_id="conv-owned-title", content="你好", capability_id=None)
        self.assertEqual(response.status_code, 202)

        blank = await self.client.patch("/api/v1/conversations", json={"conversation_id": "conv-owned-title", "title": "   "})
        self.assertEqual(blank.status_code, 400)

        too_long = await self.client.patch("/api/v1/conversations", json={"conversation_id": "conv-owned-title", "title": "超" * 61})
        self.assertEqual(too_long.status_code, 400)

        await self.login("bob")
        forbidden = await self.client.patch("/api/v1/conversations", json={"conversation_id": "conv-owned-title", "title": "Bob 不能改"})
        self.assertEqual(forbidden.status_code, 404)
