from __future__ import annotations

import unittest

from src.integrations.llm_runtime import SharedLLMRuntime


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


if __name__ == "__main__":
    unittest.main()
