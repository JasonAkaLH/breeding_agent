from __future__ import annotations

import unittest

from src.integrations.llm_runtime import SharedLLMRuntime
from src.orchestration.prompt_envelope import LLMMessage, PromptEnvelope, PromptSegment


class SharedLLMRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def test_reuses_one_client_for_text_and_stream_calls(self) -> None:
        class FakeClient:
            instances: list["FakeClient"] = []

            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.calls: list[tuple[str, type, str, bool, str]] = []
                FakeClient.instances.append(self)

            async def generate_text(self, prompt: str, *, thinking: bool = False, reasoning_effort: str = "minimal") -> str:
                self.calls.append(("text", type(prompt), prompt, thinking, reasoning_effort))
                return "text-output"

            async def generate_text_with_thinking(self, prompt: str, *, thinking: bool = False, reasoning_effort: str = "minimal"):
                self.calls.append(("stream", type(prompt), prompt, thinking, reasoning_effort))
                yield {"reasoning": "r", "answer": None}
                yield {"reasoning": None, "answer": "a"}

            def safe_metadata(self, *, config_source=None, reasoning_effort=None):
                return {"provider": "fake", "config_source": config_source, "reasoning_effort": reasoning_effort}

        runtime = SharedLLMRuntime(client_factory=FakeClient, config={"model": "fake"}, config_source="injected_config")
        text = await runtime.generate_text("p1", reasoning_effort="max")
        events = [event async for event in runtime.stream_events("p2", thinking=True, reasoning_effort="high")]

        self.assertEqual(text, "text-output")
        self.assertEqual(events, [{"answer": None, "reasoning": "r"}, {"answer": "a", "reasoning": None}])
        self.assertEqual(len(FakeClient.instances), 1)
        self.assertEqual(
            FakeClient.instances[0].calls,
            [("text", str, "p1", False, "max"), ("stream", str, "p2", True, "high")],
        )

    async def test_generate_text_can_collect_stream_reasoning_when_requested(self) -> None:
        class FakeClient:
            async def generate_text_with_thinking(self, prompt: str, *, thinking: bool = False, reasoning_effort: str = "minimal"):
                yield {"reasoning": "think", "answer": None}
                yield {"answer": "answer", "reasoning": None}

        runtime = SharedLLMRuntime(client=FakeClient())
        reasoning: list[str] = []

        async def record(delta: str) -> None:
            reasoning.append(delta)

        text = await runtime.generate_text("p", thinking=True, reasoning_effort="high", on_reasoning_delta=record)

        self.assertEqual(text, "answer")
        self.assertEqual(reasoning, ["think"])

    async def test_model_edition_override_uses_separate_cached_client(self) -> None:
        class FakeClient:
            instances: list["FakeClient"] = []

            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.model = kwargs["config"]["model_edition"]
                self.calls: list[str] = []
                FakeClient.instances.append(self)

            async def generate_text(self, prompt: str, *, thinking: bool = False, reasoning_effort: str = "minimal") -> str:
                self.calls.append(prompt)
                return self.model

        runtime = SharedLLMRuntime(
            client_factory=FakeClient,
            config={
                "api_key": "test",
                "base_url": "http://example.test",
                "model_editions": {
                    "default": "deepseek-v4-flash-260425",
                    "options": [
                        {"value": "deepseek-v4-flash-260425", "label": "DeepSeek V4 Flash", "trim_max_tokens": 1024000},
                        {"value": "deepseek-v4-pro-260425", "label": "DeepSeek V4 Pro", "trim_max_tokens": 2048000},
                    ],
                },
            },
            config_source="injected_config",
        )

        first = await runtime.generate_text("p1", model_edition="deepseek-v4-flash-260425")
        second = await runtime.generate_text("p2", model_edition="deepseek-v4-flash-260425")
        third = await runtime.generate_text("p3", model_edition="deepseek-v4-pro-260425")

        self.assertEqual(first, "deepseek-v4-flash-260425")
        self.assertEqual(second, "deepseek-v4-flash-260425")
        self.assertEqual(third, "deepseek-v4-pro-260425")
        self.assertEqual([client.model for client in FakeClient.instances], ["deepseek-v4-flash-260425", "deepseek-v4-pro-260425"])
        self.assertEqual([client.kwargs["config"]["trim_max_tokens"] for client in FakeClient.instances], [1024000, 2048000])
        self.assertEqual(FakeClient.instances[0].calls, ["p1", "p2"])

    async def test_model_edition_options_do_not_inherit_top_level_trim_budget(self) -> None:
        class FakeClient:
            instances: list["FakeClient"] = []

            def __init__(self, **kwargs):
                self.kwargs = kwargs
                FakeClient.instances.append(self)

            async def generate_text(self, prompt: str, *, thinking: bool = False, reasoning_effort: str = "minimal") -> str:
                return "ok"

        runtime = SharedLLMRuntime(
            client_factory=FakeClient,
            config={
                "api_key": "test",
                "base_url": "http://example.test",
                "trim_max_tokens": 123,
                "model_editions": {
                    "default": "deepseek-v4-flash-260425",
                    "options": [{"value": "deepseek-v4-flash-260425", "label": "DeepSeek V4 Flash"}],
                },
            },
            config_source="injected_config",
        )

        await runtime.generate_text("p", model_edition="deepseek-v4-flash-260425")

        self.assertNotIn("trim_max_tokens", FakeClient.instances[0].kwargs["config"])

    async def test_static_metadata_uses_same_messages_role_alias_as_llm_client(self) -> None:
        runtime = SharedLLMRuntime(
            config={
                "model": "fake",
                "messages": {
                    "supports_messages": True,
                    "roles": ["system", "developer", "user"],
                },
            }
        )

        metadata = runtime.static_metadata()

        self.assertEqual(
            metadata["provider_role_capabilities"],
            {"supports_messages": True, "roles": ["system", "developer", "user"]},
        )

    async def test_generate_text_passes_native_messages_to_message_aware_clients(self) -> None:
        messages = (
            LLMMessage(role="system", content="rules"),
            LLMMessage(role="user", content="question"),
        )

        class FakeClient:
            def __init__(self) -> None:
                self.seen_prompt = None

            async def generate_text(self, prompt, *, thinking: bool = False, reasoning_effort: str = "minimal") -> str:
                self.seen_prompt = prompt
                return "ok"

        client = FakeClient()
        runtime = SharedLLMRuntime(client=client)

        text = await runtime.generate_text(messages)

        self.assertEqual(text, "ok")
        self.assertIs(client.seen_prompt, messages)

    async def test_stream_events_passes_messages_and_keeps_reasoning_event_shape(self) -> None:
        messages = (LLMMessage(role="user", content="question"),)

        class FakeClient:
            def __init__(self) -> None:
                self.seen_prompt = None

            async def generate_text_with_thinking(self, prompt, *, thinking: bool = False, reasoning_effort: str = "minimal"):
                self.seen_prompt = prompt
                yield {"reasoning": "think", "answer": None}
                yield {"reasoning": None, "answer": "answer"}

        client = FakeClient()
        runtime = SharedLLMRuntime(client=client)

        events = [event async for event in runtime.stream_events(messages, thinking=True, reasoning_effort="max")]

        self.assertIs(client.seen_prompt, messages)
        self.assertEqual(events, [{"answer": None, "reasoning": "think"}, {"answer": "answer", "reasoning": None}])

    async def test_prompt_envelope_renders_to_string_for_legacy_fake_clients(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.seen_prompt = None

            async def generate_text(self, prompt, *, thinking: bool = False, reasoning_effort: str = "minimal") -> str:
                self.seen_prompt = prompt
                return "ok"

        client = FakeClient()
        runtime = SharedLLMRuntime(client=client)
        envelope = PromptEnvelope(
            template_id="runtime.legacy",
            template_version="v1",
            model_edition="fake",
            trim_max_tokens=4_000,
            segments=(
                PromptSegment(
                    name="stable",
                    role="system",
                    content="系统规则",
                    priority=0,
                    mutability="stable",
                    cache_affinity="prefix",
                    trim_policy="required",
                    security_role="instruction",
                ),
                PromptSegment(
                    name="user",
                    role="user",
                    content="用户问题",
                    priority=0,
                    mutability="dynamic",
                    cache_affinity="no_cache",
                    trim_policy="required",
                    security_role="user_input",
                ),
            ),
        )

        await runtime.generate_text(envelope)

        self.assertIsInstance(client.seen_prompt, str)
        self.assertIn("系统规则", client.seen_prompt)
        self.assertIn("用户问题", client.seen_prompt)


if __name__ == "__main__":
    unittest.main()
