from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from src.integrations.llm_client import CONFIG_ENV_PREFIX, LLMClient, bootstrap_config_env, load_config
from src.orchestration.prompt_envelope import LLMMessage, PromptEnvelope, PromptSegment


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


def _assert_single_user_prompt_message(testcase: unittest.TestCase, call: dict, prompt: str = "prompt") -> None:
    testcase.assertEqual(call["messages"], [{"role": "user", "content": prompt}])
    testcase.assertEqual([message["role"] for message in call["messages"]], ["user"])


@contextmanager
def _isolated_config_env():
    saved = {key: value for key, value in os.environ.items() if key.startswith(CONFIG_ENV_PREFIX)}
    for key in list(os.environ):
        if key.startswith(CONFIG_ENV_PREFIX):
            del os.environ[key]
    try:
        yield
    finally:
        for key in list(os.environ):
            if key.startswith(CONFIG_ENV_PREFIX):
                del os.environ[key]
        os.environ.update(saved)


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
                bootstrap_config_env(path, override=True)

    def test_bootstrap_config_env_loads_yaml_once_for_default_client(self) -> None:
        with _isolated_config_env(), tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "config.yaml"
            path.write_text(
                "\n".join(
                    [
                        "api_key: test-key",
                        "base_url: https://example.test/v1",
                        "model_edition: env-model",
                        "temperature: 0",
                        "max_retries: 0",
                        "timeout: 1",
                        "trim_max_tokens: 123",
                    ]
                ),
                encoding="utf-8",
            )

            bootstrap_config_env(path, override=True)
            self.assertEqual(os.environ["MAF_CONFIG_MODEL_EDITION"], "env-model")
            self.assertEqual(load_config()["trim_max_tokens"], 123)

            path.unlink()
            client = LLMClient()

        self.assertEqual(client.model, "env-model")

    def test_explicit_config_overrides_bootstrapped_environment(self) -> None:
        with _isolated_config_env(), tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "config.yaml"
            path.write_text(
                "\n".join(
                    [
                        "api_key: env-key",
                        "base_url: https://env.example.test/v1",
                        "model: env-model",
                    ]
                ),
                encoding="utf-8",
            )
            bootstrap_config_env(path, override=True)

            client = LLMClient(
                config={
                    "api_key": "injected-key",
                    "base_url": "https://injected.example.test/v1",
                    "model": "injected-model",
                    "temperature": 0,
                    "max_retries": 0,
                    "timeout": 1,
                }
            )

        self.assertEqual(client.model, "injected-model")

    def test_bootstrap_config_env_clears_stale_values_when_source_changes(self) -> None:
        with _isolated_config_env(), tempfile.TemporaryDirectory() as tmpdir:
            first_path = Path(tmpdir) / "first.yaml"
            first_path.write_text(
                "\n".join(
                    [
                        "api_key: first-key",
                        "base_url: https://first.example.test/v1",
                        "model: first-model",
                        "trim_max_tokens: 123",
                    ]
                ),
                encoding="utf-8",
            )
            second_path = Path(tmpdir) / "second.yaml"
            second_path.write_text(
                "\n".join(
                    [
                        "api_key: second-key",
                        "base_url: https://second.example.test/v1",
                        "model: second-model",
                    ]
                ),
                encoding="utf-8",
            )

            bootstrap_config_env(first_path, override=True)
            self.assertEqual(load_config()["trim_max_tokens"], 123)

            bootstrap_config_env(second_path, override=True)
            loaded_config = load_config()
            client = LLMClient()

        self.assertEqual(loaded_config["model"], "second-model")
        self.assertNotIn("trim_max_tokens", loaded_config)
        self.assertEqual(client.model, "second-model")

    def test_llm_client_config_path_switches_to_requested_file(self) -> None:
        with _isolated_config_env(), tempfile.TemporaryDirectory() as tmpdir:
            first_path = Path(tmpdir) / "first.yaml"
            first_path.write_text(
                "\n".join(
                    [
                        "api_key: first-key",
                        "base_url: https://first.example.test/v1",
                        "model: first-model",
                    ]
                ),
                encoding="utf-8",
            )
            second_path = Path(tmpdir) / "second.yaml"
            second_path.write_text(
                "\n".join(
                    [
                        "api_key: second-key",
                        "base_url: https://second.example.test/v1",
                        "model: second-model",
                    ]
                ),
                encoding="utf-8",
            )

            first_client = LLMClient(config_path=first_path)
            second_client = LLMClient(config_path=second_path)

        self.assertEqual(first_client.model, "first-model")
        self.assertEqual(second_client.model, "second-model")

    def test_llm_client_config_path_overrides_rewritten_same_source(self) -> None:
        with _isolated_config_env(), tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "config.yaml"
            path.write_text(
                "\n".join(
                    [
                        "api_key: first-key",
                        "base_url: https://first.example.test/v1",
                        "model: first-model",
                        "trim_max_tokens: 123",
                    ]
                ),
                encoding="utf-8",
            )
            first_client = LLMClient(config_path=path)

            path.write_text(
                "\n".join(
                    [
                        "api_key: second-key",
                        "base_url: https://second.example.test/v1",
                        "model: second-model",
                    ]
                ),
                encoding="utf-8",
            )
            second_client = LLMClient(config_path=path)
            loaded_config = load_config()

        self.assertEqual(first_client.model, "first-model")
        self.assertEqual(second_client.model, "second-model")
        self.assertNotIn("trim_max_tokens", loaded_config)

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
                    reasoning_effort="max",
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
        self.assertEqual(call["reasoning_effort"], "max")
        self.assertEqual(call["extra_body"], {"thinking": {"type": "enabled"}})
        self.assertTrue(call["stream"])
        _assert_single_user_prompt_message(self, call)

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
        _assert_single_user_prompt_message(self, call)

    def test_stream_text_can_enable_thinking_for_main_agent_output(self) -> None:
        client = self.make_client()
        fake_completions = _FakeCompletions(
            [
                _chunk(reasoning="reasoning should stay hidden"),
                _chunk(answer="answer"),
            ]
        )
        client.client = SimpleNamespace(chat=SimpleNamespace(completions=fake_completions))

        async def collect() -> list[str]:
            return [
                chunk
                async for chunk in client.stream_text(
                    "prompt",
                    reasoning_effort="high",
                    thinking=True,
                )
            ]

        chunks = asyncio.run(collect())

        self.assertEqual(chunks, ["answer"])
        call = fake_completions.calls[0]
        self.assertEqual(call["reasoning_effort"], "high")
        self.assertEqual(call["extra_body"], {"thinking": {"type": "enabled"}})
        self.assertTrue(call["stream"])
        _assert_single_user_prompt_message(self, call)

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
        _assert_single_user_prompt_message(self, call)

    def test_generate_text_defaults_to_minimal_reasoning_without_thinking(self) -> None:
        client = self.make_client()
        fake_completions = _FakeCompletions(response=_completion("OK"))
        client.client = SimpleNamespace(chat=SimpleNamespace(completions=fake_completions))

        answer = asyncio.run(client.generate_text("prompt"))

        self.assertEqual(answer, "OK")
        call = fake_completions.calls[0]
        self.assertFalse(call["stream"])
        self.assertEqual(call["reasoning_effort"], "minimal")
        self.assertEqual(call["extra_body"], {"thinking": {"type": "disabled"}})
        _assert_single_user_prompt_message(self, call)

    def test_generate_text_accepts_messages_and_falls_back_unsupported_roles_deterministically(self) -> None:
        client = LLMClient(
            config={
                "api_key": "test-key",
                "base_url": "https://example.test/v1",
                "model": "test-model",
                "temperature": 0,
                "max_retries": 0,
                "timeout": 1,
                "provider_role_capabilities": {"roles": ["system", "user"]},
            }
        )
        fake_completions = _FakeCompletions(response=_completion("OK"))
        client.client = SimpleNamespace(chat=SimpleNamespace(completions=fake_completions))

        answer = asyncio.run(
            client.generate_text(
                [
                    LLMMessage(role="developer", content="developer contract"),
                    LLMMessage(role="tool", content="tool result"),
                    LLMMessage(role="user", content="user asks"),
                ]
            )
        )

        self.assertEqual(answer, "OK")
        call = fake_completions.calls[0]
        self.assertEqual([message["role"] for message in call["messages"]], ["system", "user", "user"])
        self.assertIn("role_fallback:developer", call["messages"][0]["content"])
        self.assertIn("role_fallback:tool", call["messages"][1]["content"])
        self.assertIn("不是用户指令", call["messages"][1]["content"])
        self.assertEqual(call["messages"][2], {"role": "user", "content": "user asks"})
        self.assertEqual(
            client.last_message_role_fallbacks,
            (
                {
                    "segment_name": "message_0",
                    "source_role": "developer",
                    "target_role": "system",
                    "reason": "developer_to_system",
                },
                {
                    "segment_name": "message_1",
                    "source_role": "tool",
                    "target_role": "user",
                    "reason": "tool_to_user_context",
                },
            ),
        )

    def test_generate_text_preserves_configured_extended_roles_when_provider_declares_support(self) -> None:
        client = LLMClient(
            config={
                "api_key": "test-key",
                "base_url": "https://example.test/v1",
                "model": "test-model",
                "temperature": 0,
                "max_retries": 0,
                "timeout": 1,
                "provider_role_capabilities": {"roles": ["system", "developer", "user", "tool"]},
            }
        )
        fake_completions = _FakeCompletions(response=_completion("OK"))
        client.client = SimpleNamespace(chat=SimpleNamespace(completions=fake_completions))

        asyncio.run(
            client.generate_text(
                [
                    LLMMessage(role="developer", content="developer contract"),
                    LLMMessage(role="tool", content="tool result", name="tool_1"),
                    LLMMessage(role="user", content="user asks"),
                ]
            )
        )

        call = fake_completions.calls[0]
        self.assertEqual([message["role"] for message in call["messages"]], ["developer", "tool", "user"])
        self.assertEqual(call["messages"][1]["name"], "tool_1")

    def test_generate_text_collapses_messages_to_single_user_block_when_messages_are_disabled(self) -> None:
        client = LLMClient(
            config={
                "api_key": "test-key",
                "base_url": "https://example.test/v1",
                "model": "test-model",
                "temperature": 0,
                "max_retries": 0,
                "timeout": 1,
                "provider_role_capabilities": {"supports_messages": False, "roles": ["system", "user"]},
            }
        )
        fake_completions = _FakeCompletions(response=_completion("OK"))
        client.client = SimpleNamespace(chat=SimpleNamespace(completions=fake_completions))

        asyncio.run(
            client.generate_text(
                [
                    LLMMessage(role="system", content="system contract"),
                    LLMMessage(role="user", content="user asks"),
                ]
            )
        )

        call = fake_completions.calls[0]
        self.assertEqual(len(call["messages"]), 1)
        self.assertEqual(call["messages"][0]["role"], "user")
        self.assertIn("# role:system", call["messages"][0]["content"])
        self.assertIn("system contract", call["messages"][0]["content"])
        self.assertEqual(
            client.last_message_role_fallbacks,
            (
                {
                    "segment_name": "message_0",
                    "source_role": "system",
                    "target_role": "user",
                    "reason": "messages_disabled_to_user_context",
                },
            ),
        )

    def test_provider_feature_capabilities_can_omit_thinking_and_reasoning_options(self) -> None:
        client = LLMClient(
            config={
                "api_key": "test-key",
                "base_url": "https://example.test/v1",
                "model": "test-model",
                "temperature": 0,
                "max_retries": 0,
                "timeout": 1,
                "provider_feature_capabilities": {
                    "supports_thinking": False,
                    "supports_reasoning_effort": False,
                },
            }
        )
        fake_completions = _FakeCompletions(response=_completion("OK"))
        client.client = SimpleNamespace(chat=SimpleNamespace(completions=fake_completions))

        asyncio.run(client.generate_text([LLMMessage(role="user", content="prompt")], thinking=False, reasoning_effort="minimal"))

        call = fake_completions.calls[0]
        self.assertNotIn("extra_body", call)
        self.assertNotIn("reasoning_effort", call)
        self.assertEqual(client.safe_metadata()["provider_feature_capabilities"]["supports_thinking"], False)

    def test_prompt_envelope_input_renders_to_messages_with_final_preflight(self) -> None:
        client = self.make_client()
        fake_completions = _FakeCompletions(response=_completion("OK"))
        client.client = SimpleNamespace(chat=SimpleNamespace(completions=fake_completions))

        asyncio.run(
            client.generate_text(
                PromptEnvelope(
                    template_id="client.messages",
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
                            name="tool_context",
                            role="tool",
                            content="工具结果",
                            priority=0,
                            mutability="dynamic",
                            cache_affinity="no_cache",
                            trim_policy="compressible",
                            security_role="tool_result",
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
            )
        )

        call = fake_completions.calls[0]
        self.assertEqual([message["role"] for message in call["messages"]], ["system", "user", "user"])
        self.assertIn("工具结果", call["messages"][1]["content"])

    def test_safe_metadata_excludes_secrets_and_raw_endpoint(self) -> None:
        client = self.make_client()

        metadata = client.safe_metadata(config_source="injected_config", reasoning_effort="minimal")

        self.assertEqual(metadata["provider"], "openai_compatible")
        self.assertEqual(metadata["model"], "test-model")
        self.assertEqual(metadata["reasoning_effort"], "minimal")
        self.assertEqual(metadata["config_source"], "injected_config")
        self.assertTrue(metadata["base_url_configured"])
        self.assertEqual(metadata["provider_role_capabilities"]["supports_messages"], True)
        self.assertEqual(set(metadata["provider_role_capabilities"]["roles"]), {"system", "user"})
        self.assertNotIn("api_key", metadata)
        self.assertNotIn("base_url", metadata)

    def test_streaming_with_messages_preserves_reasoning_and_answer_chunks(self) -> None:
        client = self.make_client()
        fake_completions = _FakeCompletions(
            [
                _chunk(reasoning="先分析 messages"),
                _chunk(answer="hello"),
            ]
        )
        client.client = SimpleNamespace(chat=SimpleNamespace(completions=fake_completions))

        async def collect() -> list[dict[str, str | None]]:
            return [
                event
                async for event in client.generate_text_with_thinking(
                    [LLMMessage(role="system", content="rules"), LLMMessage(role="user", content="prompt")],
                    thinking=True,
                    reasoning_effort="high",
                )
            ]

        events = asyncio.run(collect())

        self.assertEqual(events, [{"answer": None, "reasoning": "先分析 messages"}, {"answer": "hello", "reasoning": None}])
        call = fake_completions.calls[0]
        self.assertEqual([message["role"] for message in call["messages"]], ["system", "user"])
        self.assertEqual(call["extra_body"], {"thinking": {"type": "enabled"}})


if __name__ == "__main__":
    unittest.main()
