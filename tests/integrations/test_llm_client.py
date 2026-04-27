from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from src.integrations.llm_client import LLMClient, load_config


class _FakeStream:
    def __init__(self, chunks: list[object]) -> None:
        self._chunks = chunks

    def __aiter__(self):
        self._iter = iter(self._chunks)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class _FakeCompletions:
    def __init__(self, chunks: list[object] | None = None, response: object | None = None) -> None:
        self.calls: list[dict] = []
        self._chunks = chunks or []
        self._response = response

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("stream"):
            return _FakeStream(self._chunks)
        if self._response is not None:
            return self._response
        return _FakeStream(self._chunks)


def _chunk(*, answer: str | None = None, reasoning: str | None = None) -> object:
    delta = SimpleNamespace(content=answer, reasoning_content=reasoning)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)])


def _completion(answer: str | None) -> object:
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=answer))])


class LLMClientTest(unittest.TestCase):
    def make_client(self) -> LLMClient:
        return LLMClient(
            config={
                "api_key": "test-key",
                "base_url": "https://example.test/v1",
                "model": "test-model",
                "temperature": 0,
                "max_retries": 0,
                "timeout": 1,
            }
        )

    def test_load_config_requires_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "config.yaml"
            path.write_text("- not\n- a\n- mapping\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "must be a mapping"):
                load_config(path)

    def test_streaming_extracts_reasoning_and_answer_chunks(self) -> None:
        client = self.make_client()
        fake_completions = _FakeCompletions(
            [
                _chunk(reasoning="先分析"),
                _chunk(answer="hello"),
                _chunk(answer=" world"),
            ]
        )
        client.client = SimpleNamespace(chat=SimpleNamespace(completions=fake_completions))

        async def collect() -> list[dict[str, str | None]]:
            return [
                event
                async for event in client.generate_text_with_thinking(
                    "prompt",
                    thinking=True,
                    reasoning_effort="low",
                )
            ]

        events = asyncio.run(collect())

        self.assertEqual(
            events,
            [
                {"answer": None, "reasoning": "先分析"},
                {"answer": "hello", "reasoning": None},
                {"answer": " world", "reasoning": None},
            ],
        )
        call = fake_completions.calls[0]
        self.assertEqual(call["reasoning_effort"], "low")
        self.assertEqual(call["extra_body"], {"thinking": {"type": "enabled"}})
        self.assertTrue(call["stream"])

    def test_stream_text_uses_streaming_without_thinking_for_main_agent_output(self) -> None:
        client = self.make_client()
        fake_completions = _FakeCompletions(
            [
                _chunk(reasoning="should be ignored"),
                _chunk(answer="hello"),
                _chunk(answer=" world"),
            ]
        )
        client.client = SimpleNamespace(chat=SimpleNamespace(completions=fake_completions))

        async def collect() -> list[str]:
            return [
                chunk
                async for chunk in client.stream_text(
                    "prompt",
                    reasoning_effort="minimal",
                )
            ]

        chunks = asyncio.run(collect())

        self.assertEqual(chunks, ["hello", " world"])
        call = fake_completions.calls[0]
        self.assertEqual(call["reasoning_effort"], "minimal")
        self.assertEqual(call["extra_body"], {"thinking": {"type": "disabled"}})
        self.assertTrue(call["stream"])

    def test_generate_text_uses_non_streaming_completion_for_structured_tasks(self) -> None:
        client = self.make_client()
        fake_completions = _FakeCompletions(response=_completion('{"mode":"answer"}'))
        client.client = SimpleNamespace(chat=SimpleNamespace(completions=fake_completions))

        answer = asyncio.run(client.generate_text("prompt", thinking=False, reasoning_effort="minimal"))

        self.assertEqual(answer, '{"mode":"answer"}')
        call = fake_completions.calls[0]
        self.assertFalse(call["stream"])
        self.assertEqual(call["reasoning_effort"], "minimal")
        self.assertEqual(call["extra_body"], {"thinking": {"type": "disabled"}})

    def test_safe_metadata_excludes_secrets_and_raw_endpoint(self) -> None:
        client = self.make_client()

        metadata = client.safe_metadata(config_source="injected_config", reasoning_effort="minimal")

        self.assertEqual(metadata["provider"], "openai_compatible")
        self.assertEqual(metadata["model"], "test-model")
        self.assertEqual(metadata["reasoning_effort"], "minimal")
        self.assertEqual(metadata["config_source"], "injected_config")
        self.assertTrue(metadata["base_url_configured"])
        self.assertNotIn("api_key", metadata)
        self.assertNotIn("base_url", metadata)


if __name__ == "__main__":
    unittest.main()
