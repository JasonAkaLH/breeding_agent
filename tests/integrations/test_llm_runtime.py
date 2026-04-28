from __future__ import annotations

import unittest

from src.integrations.llm_runtime import SharedLLMRuntime


class SharedLLMRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def test_reuses_one_client_for_text_and_stream_calls(self) -> None:
        class FakeClient:
            instances: list["FakeClient"] = []

            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.calls: list[str] = []
                FakeClient.instances.append(self)

            async def generate_text(self, prompt: str, *, thinking: bool = False, reasoning_effort: str = "minimal") -> str:
                self.calls.append(f"text:{prompt}:{thinking}:{reasoning_effort}")
                return "text-output"

            async def generate_text_with_thinking(self, prompt: str, *, thinking: bool = False, reasoning_effort: str = "minimal"):
                self.calls.append(f"stream:{prompt}:{thinking}:{reasoning_effort}")
                yield {"reasoning": "r", "answer": None}
                yield {"reasoning": None, "answer": "a"}

            def safe_metadata(self, *, config_source=None, reasoning_effort=None):
                return {"provider": "fake", "config_source": config_source, "reasoning_effort": reasoning_effort}

        runtime = SharedLLMRuntime(client_factory=FakeClient, config={"model": "fake"}, config_source="injected_config")
        text = await runtime.generate_text("p1", reasoning_effort="low")
        events = [event async for event in runtime.stream_events("p2", thinking=True, reasoning_effort="high")]

        self.assertEqual(text, "text-output")
        self.assertEqual(events, [{"answer": None, "reasoning": "r"}, {"answer": "a", "reasoning": None}])
        self.assertEqual(len(FakeClient.instances), 1)
        self.assertEqual(FakeClient.instances[0].calls, ["text:p1:False:low", "stream:p2:True:high"])

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


if __name__ == "__main__":
    unittest.main()
