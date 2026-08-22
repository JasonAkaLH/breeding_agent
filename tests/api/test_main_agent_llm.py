from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from datetime import datetime
from unittest.mock import patch

from tests.api.support import APITestCase
from src.api.runtime import _resolve_conversation_memory_builder, _resolve_skill_input_text_generator
from src.core.enums import MessageRole, TaskStatus
from src.core.models import Conversation, Message, Task
from src.integrations.llm_runtime import SharedLLMRuntime
from src.orchestration.models import OrchestrationRequest
from src.orchestration.prompt_envelope import LLMMessage


def _test_reasoning_efforts() -> dict:
    return {
        "default": "minimal",
        "disabled_default": "minimal",
        "options": [
            {"value": "minimal", "label": "最低", "allow_when_thinking_disabled": True},
            {"value": "max", "label": "最高", "allow_when_thinking_disabled": False},
        ],
    }


def _test_model_editions(model: str) -> dict:
    return {
        "default": model,
        "options": [
            {
                "value": model,
                "label": model,
                "reasoning_efforts": _test_reasoning_efforts(),
                "agent_capabilities": {
                    "supports_messages": True,
                    "roles": ["system", "developer", "user", "assistant", "tool"],
                    "supports_native_tools": True,
                    "supports_required_tool_choice": True,
                    "supports_streamed_tool_calls": True,
                },
            }
        ],
    }


class MainAgentLLMAPITest(APITestCase):
    async def test_default_message_uses_main_agent_and_streams_output_events(self) -> None:
        release_stream = asyncio.Event()

        async def streamer(prompt: str):
            await release_stream.wait()
            yield "你好"
            yield "，我是主代理"

        await self.reconfigure_runtime(main_agent_stream_generator=streamer, skill_roots=None)
        response = await self.client.post(
            "/api/v1/conversations/chat-messages",
            json={
                "conversation_id": "conv-main",
                "content": "你好",
                "routing_mode": "auto",
                "capability_id": None,
                "metadata": {},
            },
        )
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]

        iterator = self.runtime.iter_frontend_events(task_id).__aiter__()
        seen_types: set[str] = set()
        deltas: list[str] = []

        async def collect_events() -> None:
            while "task.completed" not in seen_types:
                event = await asyncio.wait_for(iterator.__anext__(), timeout=2)
                seen_types.add(event.event_type)
                if event.event_type == "main_agent.output_delta":
                    deltas.append(event.payload["delta"])

        collector = asyncio.create_task(collect_events())
        await asyncio.sleep(0.05)
        release_stream.set()
        await collector

        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")
        self.assertEqual(terminal["completed_node_count"], 1)

        self.assertEqual(deltas, ["你好", "，我是主代理"])
        self.assertIn("main_agent.output_final", seen_types)
        persisted_events = await self.runtime.storage.list_events_for_task(task_id)
        self.assertFalse(any(event.event_type == "main_agent.output_delta" for event in persisted_events))
        self.assertTrue(any(event.event_type == "main_agent.output_final" for event in persisted_events))

    async def test_main_agent_reasoning_content_is_exposed_as_frontend_events(self) -> None:
        release_stream = asyncio.Event()

        async def streamer(prompt: str, *, reasoning_effort: str = "minimal", thinking: bool = False):
            await release_stream.wait()
            yield {"reasoning": "先分析", "answer": None}
            yield {"answer": "最终回答", "reasoning": None}

        await self.reconfigure_runtime(main_agent_stream_generator=streamer, skill_roots=None)
        response = await self.client.post(
            "/api/v1/conversations/chat-messages",
            json={
                "conversation_id": "conv-main-reasoning",
                "content": "请深度思考",
                "routing_mode": "auto",
                "capability_id": None,
                "metadata": {"deep_thinking": True},
            },
        )
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]

        iterator = self.runtime.iter_frontend_events(task_id).__aiter__()
        seen_types: set[str] = set()
        frontend_reasoning: list[str] = []
        frontend_answer: list[str] = []

        async def collect_events() -> None:
            while "task.completed" not in seen_types:
                event = await asyncio.wait_for(iterator.__anext__(), timeout=2)
                seen_types.add(event.event_type)
                if event.event_type == "main_agent.reasoning_delta":
                    frontend_reasoning.append(event.payload["delta"])
                if event.event_type == "main_agent.output_delta":
                    frontend_answer.append(event.payload["delta"])

        collector = asyncio.create_task(collect_events())
        await asyncio.sleep(0.05)
        release_stream.set()
        await collector

        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")

        self.assertEqual(frontend_reasoning, ["先分析"])
        self.assertEqual(frontend_answer, ["最终回答"])
        events = await self.runtime.storage.list_events_for_task(task_id)
        self.assertFalse(any(event.event_type == "main_agent.reasoning_delta" for event in events))
        self.assertFalse(any(event.event_type == "main_agent.output_delta" for event in events))
        self.assertTrue(
            all(
                event.created_at is not None
                for event in events
                if event.event_type in {"main_agent.output_final"}
            )
        )

    async def test_llm_planner_reasoning_content_is_exposed_as_frontend_events(self) -> None:
        release_planner = asyncio.Event()
        planner_options: list[tuple[bool, str]] = []

        class FakePlannerLLMClient:
            def __init__(self, **_kwargs) -> None:
                pass

            async def generate_text(
                self,
                _prompt: str,
                *,
                thinking: bool = False,
                reasoning_effort: str = "minimal",
            ) -> str:
                planner_options.append((thinking, reasoning_effort))
                return json.dumps({"nodes": [{"node_id": "answer_user", "capability_id": "main_agent.respond"}]})

            async def generate_text_with_thinking(
                self,
                _prompt: str,
                *,
                thinking: bool = False,
                reasoning_effort: str = "minimal",
            ) -> AsyncIterator[dict[str, str | None]]:
                planner_options.append((thinking, reasoning_effort))
                await release_planner.wait()
                if thinking:
                    yield {"reasoning": "规划选择主代理", "answer": None}
                answer = json.dumps(
                    {"nodes": [{"node_id": "answer_user", "capability_id": "main_agent.respond"}]}
                )
                yield {
                    "reasoning": None,
                    "answer": answer,
                }

        async def streamer(_prompt: str):
            yield "最终回答"

        await self.reconfigure_runtime(
            main_agent_stream_generator=streamer,
            planner_llm_config={
                "api_key": "secret-test-key",
                "base_url": "https://example.test/v1",
                "model": "fake-planner-model",
                "model_editions": _test_model_editions("fake-planner-model"),
            },
            planner_llm_client_factory=FakePlannerLLMClient,
            skill_roots=None,
        )
        response = await self.submit_message(
            conversation_id="conv-planner-reasoning",
            content="请规划后回答",
            capability_id=None,
            metadata={"deep_thinking": True, "main_agent_reasoning_effort": "max"},
        )
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]

        iterator = self.runtime.iter_frontend_events(task_id).__aiter__()
        seen_types: set[str] = set()
        planner_reasoning: list[str] = []

        async def collect_events() -> None:
            while "task.completed" not in seen_types:
                event = await asyncio.wait_for(iterator.__anext__(), timeout=2)
                seen_types.add(event.event_type)
                if event.event_type == "planner.reasoning_delta":
                    planner_reasoning.append(event.payload["delta"])

        collector = asyncio.create_task(collect_events())
        await asyncio.sleep(0.05)
        release_planner.set()
        await collector

        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")

        self.assertEqual(planner_options, [(True, "max")])
        self.assertEqual(planner_reasoning, ["规划选择主代理"])
        self.assertIn("planner.reasoning_delta", seen_types)
        events = await self.runtime.storage.list_events_for_task(task_id)
        self.assertFalse(any(event.event_type == "planner.reasoning_delta" for event in events))
        plan_event = next(event for event in events if event.event_type == "workflow.plan_built")
        self.assertEqual(plan_event.payload["metadata"]["route"], "llm_planner")

    async def test_skill_input_llm_reasoning_content_is_exposed_as_interrupt_events(self) -> None:
        published = []

        class FakeSkillInputLLMClient:
            async def generate_text(self, _prompt: str, **_kwargs) -> str:
                return "fallback"

            async def generate_text_with_thinking(
                self,
                _prompt: str,
                *,
                thinking: bool = False,
                reasoning_effort: str = "minimal",
            ) -> AsyncIterator[dict[str, str | None]]:
                self.seen_options = {"thinking": thinking, "reasoning_effort": reasoning_effort}
                yield {"reasoning": "理解补参内容", "answer": None}
                yield {"reasoning": None, "answer": '{"intent":"slot_answer"}'}

        fake_client = FakeSkillInputLLMClient()

        async def publish(event):
            published.append(event)

        generator = _resolve_skill_input_text_generator(
            skill_input_text_generator=None,
            main_agent_llm_runtime=SharedLLMRuntime(client=fake_client),
            enable_skill_input_llm=True,
            reasoning_event_publisher=publish,
        )
        self.assertIsNotNone(generator)
        assert generator is not None

        response = await generator(
            "prompt",
            metadata={"deep_thinking": True, "main_agent_reasoning_effort": "max"},
            reasoning_context={
                "event_type": "interrupt.reasoning_delta",
                "task_id": "task-skill-input",
                "conversation_id": "conv-skill-input",
                "node_id": "node-interrupt",
                "stage": "slot_extraction",
                "response_role": "interrupt",
                "interrupt_id": "interrupt-1",
                "slot_collection_id": "slot-1",
                "capability_id": "skill.demo",
            },
        )

        self.assertEqual(response, '{"intent":"slot_answer"}')
        self.assertEqual(fake_client.seen_options, {"thinking": True, "reasoning_effort": "max"})
        self.assertEqual([event.event_type for event in published], ["interrupt.reasoning_delta"])
        self.assertEqual(published[0].payload["delta"], "理解补参内容")
        self.assertEqual(published[0].payload["stage"], "slot_extraction")
        self.assertEqual(published[0].payload["slot_collection_id"], "slot-1")
        self.assertEqual(published[0].visibility.value, "frontend")

    async def test_conversation_memory_resolution_reasoning_content_is_exposed_as_frontend_events(self) -> None:
        published = []

        class FakeMemoryLLMClient:
            def __init__(self, **_kwargs) -> None:
                pass

            async def generate_text(self, _prompt: str, **_kwargs) -> str:
                return json.dumps({"should_resolve": False, "confidence": "high", "risk_flags": []})

            async def generate_text_with_thinking(
                self,
                _prompt: str,
                *,
                thinking: bool = False,
                reasoning_effort: str = "minimal",
            ) -> AsyncIterator[dict[str, str | None]]:
                if thinking:
                    yield {"reasoning": f"记忆解析:{reasoning_effort}", "answer": None}
                yield {
                    "reasoning": None,
                    "answer": json.dumps(
                        {
                            "should_resolve": False,
                            "resolved_user_message": None,
                            "referenced_entity": None,
                            "entity_type": None,
                            "source": {},
                            "confidence": "high",
                            "reason": "current_message_complete",
                            "risk_flags": ["current_message_complete"],
                        },
                        ensure_ascii=False,
                    ),
                }

        async def publish(event):
            published.append(event)

        builder = _resolve_conversation_memory_builder(
            storage=self.runtime.storage,
            conversation_memory_builder=None,
            main_agent_llm_runtime=SharedLLMRuntime(client_factory=FakeMemoryLLMClient),
            enable_conversation_memory=True,
            resolution_generator=None,
            enable_resolution_llm=True,
            reasoning_event_publisher=publish,
            model_edition_config=None,
        )
        self.assertIsNotNone(builder)
        assert builder is not None

        await self.runtime.storage.save_conversation(Conversation("conv-memory-reasoning", "acc-1"))
        await self.runtime.storage.save_message(
            Message(
                "msg-prev-user",
                "conv-memory-reasoning",
                MessageRole.USER,
                "上一轮：材料是A、B、C。",
                created_at=datetime(2026, 6, 11, 1, 0, 0),
            )
        )
        await self.runtime.storage.save_message(
            Message(
                "msg-current",
                "conv-memory-reasoning",
                MessageRole.USER,
                "给我设计一个随机区组",
                task_id="task-memory-reasoning",
                created_at=datetime(2026, 6, 11, 1, 1, 0),
            )
        )

        context = await builder.build(
            OrchestrationRequest(
                task_id="task-memory-reasoning",
                conversation_id="conv-memory-reasoning",
                root_message_id="msg-current",
                user_message="给我设计一个随机区组",
                metadata={"deep_thinking": True, "main_agent_reasoning_effort": "max"},
            ),
            username="acc-1",
        )

        self.assertEqual(context.current_user_message, "给我设计一个随机区组")
        self.assertEqual([event.event_type for event in published], ["memory.reasoning_delta"])
        self.assertEqual(published[0].payload["delta"], "记忆解析:max")
        self.assertEqual(published[0].payload["stage"], "conversation_memory_resolution")
        self.assertEqual(published[0].visibility.value, "frontend")

    async def test_conversation_memory_summary_uses_disabled_default_when_request_effort_requires_thinking(self) -> None:
        calls: list[dict[str, object]] = []
        model_config = {
            "api_key": "secret-test-key",
            "base_url": "https://example.test/v1",
            "model": "fake-memory-model",
            "model_editions": _test_model_editions("fake-memory-model"),
            "trim_max_tokens": 1000,
            "conversation_memory_recent_turns": 1,
        }

        class FakeMemoryLLMClient:
            def __init__(self, **_kwargs) -> None:
                pass

            async def generate_text(
                self,
                _prompt: str,
                *,
                thinking: bool = False,
                reasoning_effort: str = "minimal",
            ) -> str:
                calls.append({"thinking": thinking, "reasoning_effort": reasoning_effort})
                return "忠实摘要：历史查询龙粳33。"

        builder = _resolve_conversation_memory_builder(
            storage=self.runtime.storage,
            conversation_memory_builder=None,
            main_agent_llm_runtime=SharedLLMRuntime(client_factory=FakeMemoryLLMClient, config=model_config),
            enable_conversation_memory=True,
            resolution_generator=None,
            enable_resolution_llm=False,
            reasoning_event_publisher=None,
            model_edition_config=model_config,
        )
        self.assertIsNotNone(builder)
        assert builder is not None

        await self.runtime.storage.save_conversation(Conversation("conv-memory-summary", "acc-1"))
        for index in range(4):
            task_id = f"task-memory-summary-{index}"
            await self.runtime.storage.save_task(
                Task(task_id, "conv-memory-summary", root_message_id=f"msg-memory-summary-{index}", status=TaskStatus.COMPLETED)
            )
            await self.runtime.storage.save_message(
                Message(
                    f"msg-memory-summary-{index}",
                    "conv-memory-summary",
                    MessageRole.USER,
                    "查询龙粳33。" + ("长上下文" * 300),
                    task_id=task_id,
                    created_at=datetime(2026, 6, 11, 1, index, 0),
                )
            )
            await self.runtime.storage.save_message(
                Message(
                    f"{task_id}:assistant",
                    "conv-memory-summary",
                    MessageRole.ASSISTANT,
                    "龙粳33答复。" + ("历史答复" * 80),
                    task_id=task_id,
                    created_at=datetime(2026, 6, 11, 1, index, 30),
                )
            )
        await self.runtime.storage.save_task(
            Task("task-memory-summary-current", "conv-memory-summary", root_message_id="msg-memory-summary-current", status=TaskStatus.ACCEPTED)
        )
        await self.runtime.storage.save_message(
            Message(
                "msg-memory-summary-current",
                "conv-memory-summary",
                MessageRole.USER,
                "继续",
                task_id="task-memory-summary-current",
                created_at=datetime(2026, 6, 11, 1, 10, 0),
            )
        )

        context = await builder.build(
            OrchestrationRequest(
                task_id="task-memory-summary-current",
                conversation_id="conv-memory-summary",
                root_message_id="msg-memory-summary-current",
                user_message="继续",
                metadata={
                    "deep_thinking": True,
                    "main_agent_reasoning_effort": "max",
                    "model_edition": "fake-memory-model",
                },
            ),
            username="acc-1",
        )

        self.assertEqual(calls, [{"thinking": False, "reasoning_effort": "minimal"}])
        self.assertEqual(context.history_summary, "忠实摘要：历史查询龙粳33。")
        self.assertNotEqual(context.fallback_reason, "summary_llm_failed")

    async def test_prompt_envelope_shadow_audit_is_not_frontend_visible(self) -> None:
        release_stream = asyncio.Event()

        async def streamer(prompt: str):
            await release_stream.wait()
            yield "shadow"
            yield " ok"

        await self.reconfigure_runtime(main_agent_stream_generator=streamer, skill_roots=None)
        with patch.dict(os.environ, {"MAF_PROMPT_ENVELOPE_MODE": "shadow"}):
            response = await self.client.post(
                "/api/v1/conversations/chat-messages",
                json={
                    "conversation_id": "conv-main-shadow-envelope",
                    "content": "你好",
                    "routing_mode": "auto",
                    "capability_id": None,
                    "metadata": {},
                },
            )
            self.assertEqual(response.status_code, 202)
            task_id = response.json()["task_id"]

            iterator = self.runtime.iter_frontend_events(task_id).__aiter__()
            seen_types: set[str] = set()
            deltas: list[str] = []

            async def collect_events() -> None:
                while "task.completed" not in seen_types:
                    event = await asyncio.wait_for(iterator.__anext__(), timeout=2)
                    seen_types.add(event.event_type)
                    if event.event_type == "main_agent.output_delta":
                        deltas.append(event.payload["delta"])

            collector = asyncio.create_task(collect_events())
            await asyncio.sleep(0.05)
            release_stream.set()
            await collector

        self.assertEqual(deltas, ["shadow", " ok"])
        self.assertNotIn("main_agent.prompt_envelope_rendered", seen_types)
        events = await self.runtime.storage.list_events_for_task(task_id)
        prompt_event = next(event for event in events if event.event_type == "main_agent.prompt_envelope_rendered")
        self.assertEqual(prompt_event.payload["prompt_render_metrics"]["mode"], "shadow")
        self.assertIn("cacheable_prefix_hash", prompt_event.payload["prompt_render_metrics"])
        self.assertTrue(any(event.event_type == "main_agent.output_final" for event in events))
        self.assertFalse(any(event.event_type == "main_agent.output_delta" for event in events))

    async def test_prompt_envelope_messages_mode_sends_native_messages_and_keeps_audit_only(self) -> None:
        prompts: list[object] = []

        async def streamer(prompt: object):
            prompts.append(prompt)
            yield "messages"
            yield " ok"

        await self.reconfigure_runtime(main_agent_stream_generator=streamer, skill_roots=None)
        with patch.dict(os.environ, {"MAF_PROMPT_ENVELOPE_MODE": "messages"}):
            response = await self.client.post(
                "/api/v1/conversations/chat-messages",
                json={
                    "conversation_id": "conv-main-messages-envelope",
                    "content": "你好 messages",
                    "routing_mode": "auto",
                    "capability_id": None,
                    "metadata": {},
                },
            )
            self.assertEqual(response.status_code, 202)
            task_id = response.json()["task_id"]
            terminal = await self.wait_for_terminal_task(task_id)

        self.assertEqual(terminal["status"], "completed")
        self.assertIsInstance(prompts[0], tuple)
        self.assertTrue(all(isinstance(message, LLMMessage) for message in prompts[0]))
        self.assertIn("你好 messages", "\n".join(message.content for message in prompts[0] if message.role == "user"))
        events = await self.runtime.storage.list_events_for_task(task_id)
        prompt_event = next(event for event in events if event.event_type == "main_agent.prompt_envelope_rendered")
        self.assertEqual(prompt_event.visibility, "audit_only")
        self.assertEqual(prompt_event.payload["mode"], "messages")
        self.assertEqual(prompt_event.payload["effective_mode"], "messages")
        self.assertLessEqual(prompt_event.payload["final_input_tokens"], prompt_event.payload["final_input_token_budget"])
        self.assertEqual(prompt_event.payload["prompt_render_metrics"]["mode"], "messages")
        llm_event = next(event for event in events if event.event_type == "main_agent.llm_call")
        self.assertEqual(llm_event.payload["prompt_envelope"]["mode"], "messages")
        self.assertEqual(
            llm_event.payload["prompt_envelope"]["prompt_render_metrics"],
            prompt_event.payload["prompt_render_metrics"],
        )
        self.assertFalse(any(event.event_type == "main_agent.output_delta" for event in events))

    async def test_explicit_generic_data_lookup_capability_runs_internal_filtering_node(self) -> None:
        response = await self.submit_message(content="查询品种龙粳33的基因型信息", capability_id="skill.generic_data_lookup")
        self.assertEqual(response.status_code, 202)
        terminal = await self.wait_for_terminal_task(response.json()["task_id"])
        self.assertEqual(terminal["status"], "completed")
        self.assertEqual(terminal["completed_node_count"], 3)

    async def test_explicit_generic_data_lookup_capability_bypasses_llm_planner(self) -> None:
        def planner(_prompt: str) -> str:
            raise AssertionError("Explicit capability routing must not call the LLM planner.")

        async def streamer(_prompt: str):
            yield "unused"

        await self.reconfigure_runtime(
            planner_text_generator=planner,
            main_agent_stream_generator=streamer,
            skill_roots=None,
        )
        response = await self.submit_message(
            conversation_id="conv-explicit-sql-bypass",
            content="查询品种龙粳33的基因型信息",
            capability_id="skill.generic_data_lookup",
        )
        self.assertEqual(response.status_code, 202)
        terminal = await self.wait_for_terminal_task(response.json()["task_id"])
        self.assertEqual(terminal["status"], "completed")
        self.assertEqual(terminal["completed_node_count"], 3)

    async def test_default_database_question_auto_builds_legacyquery_then_main_agent_dag(self) -> None:
        prompts: list[str] = []

        async def streamer(prompt: str):
            prompts.append(prompt)
            yield "这是主代理整理后的数据库答案"

        def planner(_prompt: str) -> str:
            return json.dumps({"nodes": [{"node_id": "query_data", "capability_id": "skill.generic_data_lookup"}]})

        await self.reconfigure_runtime(main_agent_stream_generator=streamer, planner_text_generator=planner, skill_roots=None)
        response = await self.client.post(
            "/api/v1/conversations/chat-messages",
            json={
                "conversation_id": "conv-auto-sql",
                "content": "查询龙粳33的详细审定信息",
                "routing_mode": "auto",
                "capability_id": None,
                "metadata": {},
            },
        )
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]

        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")
        self.assertEqual(terminal["completed_node_count"], 2)
        nodes = await self.runtime.storage.list_task_nodes_for_task(task_id)
        nodes_by_capability = {node.capability_id: node for node in nodes}
        self.assertIn("skill.generic_data_lookup", nodes_by_capability)
        self.assertIn("main_agent.respond", nodes_by_capability)
        edges = await self.runtime.storage.list_task_edges(task_id)
        self.assertIn(
            (nodes_by_capability["skill.generic_data_lookup"].node_id, nodes_by_capability["main_agent.respond"].node_id),
            {(edge.from_node_id, edge.to_node_id) for edge in edges},
        )
        self.assertIn("上游能力结果上下文", prompts[-1])
        self.assertIn("龙粳33", prompts[-1])
        self.assertIn("rows", prompts[-1])
        self.assertIn("filter_source", prompts[-1])

    async def test_default_database_question_uses_injected_llm_planner(self) -> None:
        prompts: list[str] = []
        planner_prompts: list[str] = []

        async def planner(prompt: str) -> str:
            planner_prompts.append(prompt)
            return json.dumps(
                {
                    "nodes": [
                        {"node_id": "query_data", "capability_id": "skill.generic_data_lookup"},
                        {
                            "node_id": "answer_user",
                            "capability_id": "main_agent.respond",
                            "depends_on": ["query_data"],
                        },
                    ]
                }
            )

        async def streamer(prompt: str):
            prompts.append(prompt)
            yield "这是 LLM Planner 规划后的数据库答案"

        await self.reconfigure_runtime(
            main_agent_stream_generator=streamer,
            planner_text_generator=planner,
            skill_roots=None,
        )
        response = await self.client.post(
            "/api/v1/conversations/chat-messages",
            json={
                "conversation_id": "conv-llm-planner-sql",
                "content": "查询龙粳33的详细审定信息",
                "routing_mode": "auto",
                "capability_id": None,
                "metadata": {},
            },
        )
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]

        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")
        self.assertEqual(terminal["completed_node_count"], 2)
        self.assertIn("skill.generic_data_lookup", planner_prompts[0])
        self.assertNotIn("internal.generate", planner_prompts[0])
        events = await self.runtime.storage.list_events_for_task(task_id)
        plan_event = next(event for event in events if event.event_type == "workflow.plan_built")
        self.assertEqual(plan_event.payload["metadata"]["route"], "llm_planner")
        self.assertFalse(plan_event.payload["metadata"]["planner_fallback_used"])
        self.assertIn("上游能力结果上下文", prompts[-1])

    async def test_default_message_uses_llm_planner_single_main_agent_plan(self) -> None:
        prompts: list[str] = []

        def planner(_prompt: str) -> str:
            return json.dumps({"nodes": [{"node_id": "answer_user", "capability_id": "main_agent.respond"}]})

        async def streamer(prompt: str):
            prompts.append(prompt)
            yield "planner 单主代理回答"

        await self.reconfigure_runtime(
            main_agent_stream_generator=streamer,
            planner_text_generator=planner,
            skill_roots=None,
        )
        response = await self.client.post(
            "/api/v1/conversations/chat-messages",
            json={
                "conversation_id": "conv-llm-planner-chat",
                "content": "你好，介绍一下你能做什么",
                "routing_mode": "auto",
                "capability_id": None,
                "metadata": {},
            },
        )
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]

        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")
        self.assertEqual(terminal["completed_node_count"], 1)
        self.assertIn("你好，介绍一下你能做什么", prompts[0])

    async def test_invalid_llm_planner_output_fails_after_repair_without_deterministic_auto_route(self) -> None:
        planner_prompts: list[str] = []

        async def streamer(prompt: str):
            yield "fallback answer"

        def planner(prompt: str) -> str:
            planner_prompts.append(prompt)
            return json.dumps({"nodes": [{"node_id": "bad", "capability_id": "internal.generate"}]})

        await self.reconfigure_runtime(
            main_agent_stream_generator=streamer,
            planner_text_generator=planner,
            skill_roots=None,
        )
        response = await self.client.post(
            "/api/v1/conversations/chat-messages",
            json={
                "conversation_id": "conv-llm-planner-fail",
                "content": "查询龙粳33",
                "routing_mode": "auto",
                "capability_id": None,
                "metadata": {},
            },
        )
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]

        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "failed")
        self.assertEqual(terminal["completed_node_count"], 0)
        self.assertEqual(len(planner_prompts), 2)
        self.assertIn("上一轮 Planner 输出未通过校验", planner_prompts[1])

        events = await self.runtime.storage.list_events_for_task(task_id)
        self.assertFalse(any(event.event_type == "workflow.plan_built" for event in events))
        failed_event = next(event for event in events if event.event_type == "task.failed")
        self.assertEqual(failed_event.payload["code"], "planning_failed")
        self.assertEqual(failed_event.payload["planner_reason"], "WorkflowPlanValidationError")

    async def test_llm_planner_cannot_replace_user_input_or_skip_sql_dependency(self) -> None:
        prompts: list[str] = []

        def planner(_prompt: str) -> str:
            return json.dumps(
                {
                    "nodes": [
                        {
                            "node_id": "query_data",
                            "capability_id": "skill.generic_data_lookup",
                            "input_payload": {"user_question": "恶意替换查询"},
                        },
                        {
                            "node_id": "answer_user",
                            "capability_id": "main_agent.respond",
                            "input_payload": {"user_message": "恶意替换回答"},
                        },
                    ]
                }
            )

        async def streamer(prompt: str):
            prompts.append(prompt)
            yield "安全整合回答"

        await self.reconfigure_runtime(
            main_agent_stream_generator=streamer,
            planner_text_generator=planner,
            skill_roots=None,
        )
        response = await self.submit_message(
            conversation_id="conv-planner-boundary",
            content="查询龙粳33",
            capability_id=None,
        )
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]
        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")

        nodes = await self.runtime.storage.list_task_nodes_for_task(task_id)
        nodes_by_capability = {node.capability_id: node for node in nodes}
        edges = await self.runtime.storage.list_task_edges(task_id)
        skill_node_id = nodes_by_capability["skill.generic_data_lookup"].node_id
        main_agent_targets = {
            edge.to_node_id for edge in edges if edge.from_node_id == skill_node_id
        }
        self.assertTrue(
            any(
                node.capability_id == "main_agent.respond" and node.node_id in main_agent_targets
                for node in nodes
            )
        )
        self.assertIn("查询龙粳33", prompts[-1])
        self.assertIn("上游能力结果上下文", prompts[-1])
        self.assertNotIn("恶意替换", prompts[-1])

    async def test_runtime_can_bind_llm_planner_factory_without_network(self) -> None:
        factory_kwargs: list[dict] = []
        planner_prompts: list[str] = []
        planner_reasoning_efforts: list[str] = []

        class FakePlannerLLMClient:
            def __init__(self, **kwargs) -> None:
                factory_kwargs.append(kwargs)

            async def generate_text(
                self,
                prompt: str,
                *,
                thinking: bool = False,
                reasoning_effort: str = "minimal",
            ) -> str:
                planner_prompts.append(prompt)
                planner_reasoning_efforts.append(reasoning_effort)
                return json.dumps({"nodes": [{"node_id": "answer_user", "capability_id": "main_agent.respond"}]})

        async def streamer(prompt: str):
            yield "planner factory answer"

        await self.reconfigure_runtime(
            main_agent_stream_generator=streamer,
            planner_llm_config={
                "api_key": "secret-test-key",
                "base_url": "https://example.test/v1",
                "model": "fake-planner-model",
                "model_editions": _test_model_editions("fake-planner-model"),
            },
            planner_llm_client_factory=FakePlannerLLMClient,
            planner_reasoning_effort="minimal",
            skill_roots=None,
        )
        response = await self.submit_message(
            conversation_id="conv-planner-factory",
            content="你好，主代理",
            capability_id=None,
        )
        self.assertEqual(response.status_code, 202)
        terminal = await self.wait_for_terminal_task(response.json()["task_id"])
        self.assertEqual(terminal["status"], "completed")

        self.assertEqual(factory_kwargs[0]["config"]["model"], "fake-planner-model")
        self.assertEqual(planner_reasoning_efforts, ["minimal"])
        self.assertIn("main_agent.respond", planner_prompts[0])

    async def test_rejects_old_and_internal_generic_data_lookup_capability_ids(self) -> None:
        for capability_id in ("legacy_nl_query", "legacy_nl_query.generate", "legacyquery.query", "internal.generate"):
            response = await self.submit_message(content="查询品种龙粳33的基因型信息", capability_id=capability_id)
            self.assertEqual(response.status_code, 400, capability_id)
            self.assertIn("Unsupported capability_id", response.json()["detail"])

    async def test_default_main_agent_uses_skill_catalog_from_configured_roots(self) -> None:
        skill_dir = self.workspace / "skills" / "report"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            """---
name: report-writer
description: 生成周报
triggers:
  - 周报
---

# Report Writer
请使用汇报格式。
""",
            encoding="utf-8",
        )
        prompts: list[str] = []

        async def streamer(prompt: str):
            prompts.append(prompt)
            yield "ok"

        await self.reconfigure_runtime(main_agent_stream_generator=streamer, skill_roots=[self.workspace / "skills"])
        response = await self.client.post(
            "/api/v1/conversations/chat-messages",
            json={
                "conversation_id": "conv-skill",
                "content": "帮我写周报",
                "routing_mode": "auto",
                "capability_id": None,
                "metadata": {},
            },
        )
        self.assertEqual(response.status_code, 202)
        terminal = await self.wait_for_terminal_task(response.json()["task_id"])
        self.assertEqual(terminal["status"], "completed")
        self.assertIn("report-writer", prompts[0])
        self.assertIn("生成周报", prompts[0])
        self.assertNotIn("请使用汇报格式", prompts[0])

    async def test_runtime_can_bind_main_agent_real_llm_factory_without_network(self) -> None:
        factory_kwargs: list[dict] = []
        prompts: list[str] = []
        reasoning_efforts: list[str] = []

        class FakeLLMClient:
            def __init__(self, **kwargs) -> None:
                factory_kwargs.append(kwargs)
                self.model = kwargs["config"]["model"]

            def safe_metadata(self, *, config_source: str | None = None, reasoning_effort: str | None = None) -> dict:
                return {
                    "provider": "openai_compatible",
                    "model": self.model,
                    "config_source": config_source,
                    "reasoning_effort": reasoning_effort,
                    "base_url_configured": True,
                }

            async def stream_text(self, prompt: str, *, reasoning_effort: str = "minimal") -> AsyncIterator[str]:
                prompts.append(prompt)
                reasoning_efforts.append(reasoning_effort)
                yield "真实"
                yield "LLM"

        await self.reconfigure_runtime(
            main_agent_llm_config={
                "api_key": "secret-test-key",
                "base_url": "https://example.test/v1",
                "model": "fake-main-agent-model",
                "model_editions": _test_model_editions("fake-main-agent-model"),
            },
            main_agent_llm_client_factory=FakeLLMClient,
            main_agent_reasoning_effort="minimal",
            skill_roots=None,
        )

        response = await self.submit_message(
            conversation_id="conv-main-real-llm",
            content="你好，主代理",
            capability_id=None,
        )
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]
        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")

        events = await self.runtime.storage.list_events_for_task(task_id)
        llm_event = next(event for event in events if event.event_type == "main_agent.llm_call")

        self.assertEqual(factory_kwargs[0]["config"]["model"], "fake-main-agent-model")
        self.assertEqual(reasoning_efforts, ["minimal"])
        self.assertIn("你好，主代理", prompts[0])
        self.assertEqual(llm_event.payload["model"], "fake-main-agent-model")
        self.assertEqual(llm_event.payload["config_source"], "injected_config")
        self.assertEqual(llm_event.payload["reasoning_effort"], "minimal")
        self.assertNotIn("api_key", llm_event.payload)
        self.assertNotIn("secret-test-key", str(llm_event.payload))

    async def test_metadata_controls_main_agent_thinking_and_reasoning_effort_per_request(self) -> None:
        reasoning_efforts: list[str] = []
        thinking_flags: list[bool] = []

        class FakeLLMClient:
            def __init__(self, **kwargs) -> None:
                self.model = kwargs["config"]["model"]

            def safe_metadata(self, *, config_source: str | None = None, reasoning_effort: str | None = None) -> dict:
                return {
                    "provider": "openai_compatible",
                    "model": self.model,
                    "config_source": config_source,
                    "reasoning_effort": reasoning_effort,
                }

            async def stream_text(
                self,
                prompt: str,
                *,
                reasoning_effort: str = "minimal",
                thinking: bool = False,
            ) -> AsyncIterator[str]:
                reasoning_efforts.append(reasoning_effort)
                thinking_flags.append(thinking)
                yield "深度回答"

        await self.reconfigure_runtime(
            main_agent_llm_config={
                "api_key": "secret-test-key",
                "base_url": "https://example.test/v1",
                "model": "fake-main-agent-model",
                "model_editions": _test_model_editions("fake-main-agent-model"),
            },
            main_agent_llm_client_factory=FakeLLMClient,
            main_agent_reasoning_effort="minimal",
            skill_roots=None,
        )

        response = await self.client.post(
            "/api/v1/conversations/chat-messages",
            json={
                "conversation_id": "conv-main-deep",
                "content": "请深入分析",
                "routing_mode": "auto",
                "capability_id": None,
                "metadata": {"deep_thinking": True, "main_agent_reasoning_effort": "max"},
            },
        )
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]
        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")

        events = await self.runtime.storage.list_events_for_task(task_id)
        llm_event = next(event for event in events if event.event_type == "main_agent.llm_call")
        self.assertEqual(reasoning_efforts, ["max"])
        self.assertEqual(thinking_flags, [True])
        self.assertEqual(llm_event.payload["reasoning_effort"], "max")
        self.assertTrue(llm_event.payload["thinking_enabled"])

    async def test_metadata_rejects_disallowed_reasoning_when_thinking_is_disabled(self) -> None:
        reasoning_efforts: list[str] = []
        thinking_flags: list[bool] = []

        async def streamer(
            prompt: str,
            *,
            reasoning_effort: str = "minimal",
            thinking: bool = False,
        ) -> AsyncIterator[str]:
            reasoning_efforts.append(reasoning_effort)
            thinking_flags.append(thinking)
            yield "普通回答"

        await self.reconfigure_runtime(main_agent_stream_generator=streamer, skill_roots=None)

        response = await self.client.post(
            "/api/v1/conversations/chat-messages",
            json={
                "conversation_id": "conv-main-nonthinking-effort",
                "content": "普通回答",
                "routing_mode": "auto",
                "capability_id": None,
                "metadata": {"deep_thinking": False, "main_agent_reasoning_effort": "max"},
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("does not allow reasoning_effort=max", response.json()["detail"])
        self.assertEqual(reasoning_efforts, [])
        self.assertEqual(thinking_flags, [])


if __name__ == "__main__":
    import unittest

    unittest.main()
