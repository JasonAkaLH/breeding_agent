from __future__ import annotations

import asyncio
from unittest.mock import patch

from src.core.enums import RoutingMode
from tests.api.support import APITestCase, GENERIC_DATA_SKILL_ID


class SoftSkillBindingAPITest(APITestCase):
    async def test_external_direct_skill_capability_is_rejected_before_task_creation(self) -> None:
        response = await self.client.post(
            "/api/v1/conversations/chat-messages",
            json={
                "conversation_id": "conv-direct",
                "content": "查询龙粳33",
                "routing_mode": "force_capability",
                "capability_id": GENERIC_DATA_SKILL_ID,
                "metadata": {"forced_by_slash_command": True, "slash_command": "/generic-data-lookup"},
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("direct_skill_execution_disabled", response.text)
        tasks = await self.runtime.storage.list_tasks_for_conversation("conv-direct")
        messages = await self.runtime.storage.list_messages_for_conversation("conv-direct")
        self.assertEqual(tasks, [])
        self.assertEqual(messages, [])

    async def test_external_direct_skill_is_rejected_even_when_routing_mode_auto(self) -> None:
        response = await self.client.post(
            "/api/v1/conversations/chat-messages",
            json={
                "conversation_id": "conv-direct-auto",
                "content": "查询龙粳33",
                "routing_mode": "auto",
                "capability_id": GENERIC_DATA_SKILL_ID,
                "metadata": {},
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("direct_skill_execution_disabled", response.text)

    async def test_soft_skill_binding_routes_initial_task_to_main_agent_and_executes_internal_skill(self) -> None:
        response = await self.client.post(
            "/api/v1/conversations/chat-messages",
            json={
                "conversation_id": "conv-soft",
                "content": "查询龙粳33",
                "routing_mode": "force_capability",
                "capability_id": "main_agent.respond",
                "metadata": {
                    "forced_by_slash_command": True,
                    "slash_command": "/generic-data-lookup",
                    "forced_skill_name": "malicious",
                    "soft_skill_binding": {
                        "capability_id": GENERIC_DATA_SKILL_ID,
                        "command": "/generic-data-lookup",
                    },
                },
            },
        )

        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]
        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")

        task = await self.runtime.storage.get_task(task_id)
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.routing_mode, RoutingMode.FORCE_CAPABILITY)
        self.assertEqual(task.requested_capability_id, "main_agent.respond")

        nodes = await self.runtime.storage.list_task_nodes_for_task(task_id)
        self.assertIn("main_agent.respond", [node.capability_id for node in nodes])
        self.assertIn(GENERIC_DATA_SKILL_ID, [node.capability_id for node in nodes])
        events = await self.runtime.storage.list_events_for_task(task_id)
        event_types = [event.event_type for event in events]
        self.assertIn("soft_skill_binding.decision", event_types)
        self.assertIn("skill.execution_completed", event_types)
        plan_event = next(event for event in events if event.event_type == "workflow.plan_built")
        self.assertEqual(plan_event.payload["metadata"]["skill_bundle_revision"], self.runtime._skill_runtime_state.active_revision)

    async def test_invalid_soft_skill_binding_is_rejected(self) -> None:
        response = await self.client.post(
            "/api/v1/conversations/chat-messages",
            json={
                "conversation_id": "conv-invalid-soft",
                "content": "执行",
                "routing_mode": "force_capability",
                "capability_id": "main_agent.respond",
                "metadata": {"soft_skill_binding": {"capability_id": "skill.unknown"}},
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("Unsupported soft_skill_binding capability_id", response.text)

    async def test_soft_skill_binding_answer_streams_output_deltas(self) -> None:
        async def streamer(_prompt: str, *, stage: str | None = None, **_kwargs):
            if stage == "soft_skill_decision":
                yield (
                    '{"decision":"answer","target_capability_id":"'
                    + GENERIC_DATA_SKILL_ID
                    + '","confidence":0.91,"reason_code":"usage_question"}'
                )
            elif stage == "soft_skill_answer":
                yield "这个 Skill "
                yield "需要品种名或上传数据。"
            else:
                yield "普通回答"

        await self.reconfigure_runtime(main_agent_stream_generator=streamer)
        response = await self.client.post(
            "/api/v1/conversations/chat-messages",
            json={
                "conversation_id": "conv-soft-answer-stream",
                "content": "这个 Skill 需要什么数据？",
                "routing_mode": "force_capability",
                "capability_id": "main_agent.respond",
                "metadata": {
                    "soft_skill_binding": {
                        "capability_id": GENERIC_DATA_SKILL_ID,
                        "command": "/generic-data-lookup",
                    },
                },
            },
        )

        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]
        iterator = self.runtime.iter_frontend_events(task_id).__aiter__()
        seen_types: set[str] = set()
        deltas: list[str] = []
        while "task.completed" not in seen_types:
            event = await asyncio.wait_for(iterator.__anext__(), timeout=2)
            seen_types.add(event.event_type)
            if event.event_type == "main_agent.output_delta":
                deltas.append(event.payload["delta"])

        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")
        self.assertEqual(deltas, ["这个 Skill ", "需要品种名或上传数据。"])
        persisted_events = await self.runtime.storage.list_events_for_task(task_id)
        self.assertFalse(any(event.event_type == "main_agent.output_delta" for event in persisted_events))

    async def test_soft_skill_binding_answer_records_prompt_profile_audit(self) -> None:
        async def streamer(_prompt: str, *, stage: str | None = None, **_kwargs):
            if stage == "soft_skill_decision":
                yield (
                    '{"decision":"answer","target_capability_id":"'
                    + GENERIC_DATA_SKILL_ID
                    + '","confidence":0.91,"reason_code":"usage_question"}'
                )
            elif stage == "soft_skill_answer":
                yield "公开用法说明。"
            else:
                yield "普通回答"

        await self.reconfigure_runtime(main_agent_stream_generator=streamer)
        with patch.dict("os.environ", {"MAF_PROMPT_ENVELOPE_MODE": "string"}):
            response = await self.client.post(
                "/api/v1/conversations/chat-messages",
                json={
                    "conversation_id": "conv-soft-profile",
                    "content": "这个 Skill 需要什么数据？",
                    "routing_mode": "force_capability",
                    "capability_id": "main_agent.respond",
                    "metadata": {
                        "soft_skill_binding": {
                            "capability_id": GENERIC_DATA_SKILL_ID,
                            "command": "/generic-data-lookup",
                        },
                    },
                },
            )
            self.assertEqual(response.status_code, 202, response.text)
            task_id = response.json()["task_id"]
            await self.wait_for_terminal_task(task_id)

        events = await self.runtime.storage.list_events_for_task(task_id)
        self.assertTrue(any(event.event_type == "skill.resource_read" and event.payload.get("ok") is True for event in events))
        decision_event = next(event for event in events if event.event_type == "soft_skill_binding.decision")
        profile_templates = [
            event.payload["template_id"]
            for event in events
            if event.event_type == "main_agent.prompt_profile_rendered"
        ]
        self.assertIn("soft_skill_decision", profile_templates)
        self.assertIn("soft_skill_answer", profile_templates)
        self.assertEqual(decision_event.payload["decision_prompt_profile"]["template_id"], "soft_skill_decision")
        self.assertEqual(decision_event.payload["answer_prompt_profile"]["template_id"], "soft_skill_answer")
        self.assertIn("final_input_token_budget", decision_event.payload["decision_prompt_profile"])
        self.assertIn("final_input_tokens", decision_event.payload["answer_prompt_profile"])
        self.assertNotIn("DataLookup Skill", str(decision_event.payload))

    async def test_soft_skill_decision_failure_still_persists_prompt_profile_audit(self) -> None:
        async def streamer(_prompt: str, *, stage: str | None = None, **_kwargs):
            if stage == "soft_skill_decision":
                raise RuntimeError("decision provider failed")
            yield "unreachable"

        await self.reconfigure_runtime(main_agent_stream_generator=streamer)
        with patch.dict("os.environ", {"MAF_PROMPT_ENVELOPE_MODE": "string"}):
            response = await self.client.post(
                "/api/v1/conversations/chat-messages",
                json={
                    "conversation_id": "conv-soft-profile-failed",
                    "content": "这个 Skill 需要什么数据？",
                    "routing_mode": "force_capability",
                    "capability_id": "main_agent.respond",
                    "metadata": {
                        "soft_skill_binding": {
                            "capability_id": GENERIC_DATA_SKILL_ID,
                            "command": "/generic-data-lookup",
                        },
                    },
                },
            )
            self.assertEqual(response.status_code, 202, response.text)
            task_id = response.json()["task_id"]
            terminal = await self.wait_for_terminal_task(task_id)

        self.assertEqual(terminal["status"], "failed")
        events = await self.runtime.storage.list_events_for_task(task_id)
        profile_event = next(event for event in events if event.event_type == "main_agent.prompt_profile_rendered")
        failure_event = next(event for event in events if event.event_type == "soft_skill_binding.llm_failed")
        self.assertEqual(profile_event.payload["template_id"], "soft_skill_decision")
        self.assertEqual(failure_event.payload["stage"], "soft_skill_decision")

    async def test_soft_skill_binding_followup_uses_prior_turn_from_conversation_history(self) -> None:
        seen_prompts: list[tuple[str | None, str]] = []
        answer_count = 0

        async def streamer(prompt: str, *, stage: str | None = None, **_kwargs):
            nonlocal answer_count
            if stage in {"soft_skill_decision", "soft_skill_answer"}:
                seen_prompts.append((stage, prompt))
            if stage == "soft_skill_decision":
                yield (
                    '{"decision":"answer","target_capability_id":"'
                    + GENERIC_DATA_SKILL_ID
                    + '","confidence":0.91,"reason_code":"usage_or_followup"}'
                )
            elif stage == "soft_skill_answer":
                answer_count += 1
                if answer_count == 1:
                    yield "第一轮解释：需要品种名或上传 CSV。"
                else:
                    yield "追问解释：结合上一轮，CSV 至少包含样本列。"
            else:
                yield "普通回答"

        await self.reconfigure_runtime(main_agent_stream_generator=streamer)
        first = await self.client.post(
            "/api/v1/conversations/chat-messages",
            json={
                "conversation_id": "conv-soft-followup",
                "content": "这个 Skill 需要什么数据？",
                "routing_mode": "force_capability",
                "capability_id": "main_agent.respond",
                "metadata": {
                    "soft_skill_binding": {
                        "capability_id": GENERIC_DATA_SKILL_ID,
                        "command": "/generic-data-lookup",
                    },
                },
            },
        )
        self.assertEqual(first.status_code, 202, first.text)
        await self.wait_for_terminal_task(first.json()["task_id"])

        messages = await self.runtime.storage.list_messages_for_conversation("conv-soft-followup")
        self.assertTrue(any(message.content == "这个 Skill 需要什么数据？" for message in messages))
        self.assertTrue(any(message.content == "第一轮解释：需要品种名或上传 CSV。" for message in messages))

        second = await self.client.post(
            "/api/v1/conversations/chat-messages",
            json={
                "conversation_id": "conv-soft-followup",
                "content": "再说清楚一点",
                "routing_mode": "force_capability",
                "capability_id": "main_agent.respond",
                "metadata": {
                    "soft_skill_binding": {
                        "capability_id": GENERIC_DATA_SKILL_ID,
                        "command": "/generic-data-lookup",
                    },
                },
            },
        )
        self.assertEqual(second.status_code, 202, second.text)
        await self.wait_for_terminal_task(second.json()["task_id"])

        second_turn_prompts = [prompt for _stage, prompt in seen_prompts[-2:]]
        self.assertTrue(all("对话记忆上下文" in prompt for prompt in second_turn_prompts))
        self.assertTrue(all("这个 Skill 需要什么数据？" in prompt for prompt in second_turn_prompts))
        self.assertTrue(all("第一轮解释：需要品种名或上传 CSV。" in prompt for prompt in second_turn_prompts))
