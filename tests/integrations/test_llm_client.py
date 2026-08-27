from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import yaml

from src.integrations.llm_client import CONFIG_ENV_PREFIX, LLMClient, bootstrap_config_env, load_config
from src.orchestration.prompt_envelope import LLMMessage, PromptEnvelope, PromptSegment
from src.orchestration.agent_loop.models import (
    AgentFinishMetadata,
    AgentMessage,
    AgentModelBinding,
    AgentModelRequest,
    AgentSample,
    AgentUsage,
)


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


def _reasoning_efforts() -> dict:
    return {
        "options": [
            {"value": "minimal", "label": "最低"},
            {"value": "high", "label": "高"},
            {"value": "max", "label": "最高"},
        ],
        "thinking": {
            "enabled": {"default": "minimal", "supported": ["minimal", "high", "max"]},
            "disabled": {"default": "minimal", "supported": ["minimal", "high", "max"]},
        },
    }


def _base_config(model: str = "test-model", *, model_key: str = "model", **extra: object) -> dict:
    config = {
        "api_key": "test-key",
        "base_url": "https://example.test/v1",
        model_key: model,
        "temperature": 0,
        "max_retries": 0,
        "timeout": 1,
        "model_editions": {
            "default": model,
            "options": [
                {
                    "value": model,
                    "label": model,
                    "reasoning_efforts": _reasoning_efforts(),
                    "agent_capabilities": {
                        "supports_messages": True,
                        "roles": ["system", "user", "assistant", "tool"],
                        "supports_native_tools": True,
                        "supports_required_tool_choice": True,
                        "supports_streamed_tool_calls": True,
                    },
                }
            ],
        },
    }
    config.update(extra)
    return config


def _write_config(path: Path, model: str, *, model_key: str = "model", **extra: object) -> None:
    path.write_text(
        yaml.safe_dump(_base_config(model, model_key=model_key, **extra), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


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
        return LLMClient(config=_base_config())

    def test_agent_sample_rejects_client_edition_fallback(self) -> None:
        client = self.make_client()
        request = AgentModelRequest(
            request_id="req",
            binding=AgentModelBinding("different-model"),
            messages=(AgentMessage("user", "question"),),
        )
        with self.assertRaisesRegex(ValueError, "does not match client edition"):
            asyncio.run(client.generate_agent_sample(request))

    def test_agent_sample_applies_run_bound_thinking_and_reasoning_options(self) -> None:
        client = self.make_client()
        binding = AgentModelBinding("test-model", reasoning_effort="high", thinking_enabled=True)
        request = AgentModelRequest("req", binding, (AgentMessage("user", "question"),))
        sample = AgentSample(
            sample_id="sample",
            binding=binding,
            visible_text="answer",
            tool_calls=(),
            usage=AgentUsage(),
            finish=AgentFinishMetadata(finish_reason="stop", attempts=1),
        )
        with patch("src.integrations.llm_client.OpenAIAgentModelAdapter") as adapter_type:
            adapter_type.return_value.sample_agent = AsyncMock(return_value=sample)
            result = asyncio.run(client.generate_agent_sample(request))

        self.assertIs(result, sample)
        self.assertEqual(
            adapter_type.call_args.kwargs["request_options"],
            {"extra_body": {"thinking": {"type": "enabled"}}, "reasoning_effort": "high"},
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
            _write_config(path, "env-model", model_key="model_edition", trim_max_tokens=123)

            bootstrap_config_env(path, override=True)
            self.assertEqual(os.environ["MAF_CONFIG_MODEL_EDITION"], "env-model")
            self.assertEqual(load_config()["trim_max_tokens"], 123)

            path.unlink()
            client = LLMClient()

        self.assertEqual(client.model, "env-model")

    def test_explicit_config_overrides_bootstrapped_environment(self) -> None:
        with _isolated_config_env(), tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "config.yaml"
            _write_config(path, "env-model", api_key="env-key", base_url="https://env.example.test/v1")
            bootstrap_config_env(path, override=True)

            client = LLMClient(
                config=_base_config(
                    "injected-model",
                    api_key="injected-key",
                    base_url="https://injected.example.test/v1",
                )
            )

        self.assertEqual(client.model, "injected-model")

    def test_bootstrap_config_env_clears_stale_values_when_source_changes(self) -> None:
        with _isolated_config_env(), tempfile.TemporaryDirectory() as tmpdir:
            first_path = Path(tmpdir) / "first.yaml"
            _write_config(first_path, "first-model", api_key="first-key", base_url="https://first.example.test/v1", trim_max_tokens=123)
            second_path = Path(tmpdir) / "second.yaml"
            _write_config(second_path, "second-model", api_key="second-key", base_url="https://second.example.test/v1")

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
            _write_config(first_path, "first-model", api_key="first-key", base_url="https://first.example.test/v1")
            second_path = Path(tmpdir) / "second.yaml"
            _write_config(second_path, "second-model", api_key="second-key", base_url="https://second.example.test/v1")

            first_client = LLMClient(config_path=first_path)
            second_client = LLMClient(config_path=second_path)

        self.assertEqual(first_client.model, "first-model")
        self.assertEqual(second_client.model, "second-model")

    def test_llm_client_config_path_overrides_rewritten_same_source(self) -> None:
        with _isolated_config_env(), tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "config.yaml"
            _write_config(path, "first-model", api_key="first-key", base_url="https://first.example.test/v1", trim_max_tokens=123)
            first_client = LLMClient(config_path=path)

            _write_config(path, "second-model", api_key="second-key", base_url="https://second.example.test/v1")
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

    def test_generate_text_falls_back_supported_tool_role_deterministically(self) -> None:
        client = LLMClient(config=_base_config(provider_role_capabilities={"roles": ["system", "user"]}))
        fake_completions = _FakeCompletions(response=_completion("OK"))
        client.client = SimpleNamespace(chat=SimpleNamespace(completions=fake_completions))

        answer = asyncio.run(
            client.generate_text(
                [
                    LLMMessage(role="tool", content="tool result"),
                    LLMMessage(role="user", content="user asks"),
                ]
            )
        )

        self.assertEqual(answer, "OK")
        call = fake_completions.calls[0]
        self.assertEqual([message["role"] for message in call["messages"]], ["user", "user"])
        self.assertIn("role_fallback:tool", call["messages"][0]["content"])
        self.assertIn("不是用户指令", call["messages"][0]["content"])
        self.assertEqual(call["messages"][1], {"role": "user", "content": "user asks"})
        self.assertEqual(
            client.last_message_role_fallbacks,
            (
                {
                    "segment_name": "message_0",
                    "source_role": "tool",
                    "target_role": "user",
                    "reason": "tool_to_user_context",
                },
            ),
        )

    def test_llm_message_rejects_developer_role(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unsupported LLM message role"):
            LLMMessage(role="developer", content="developer contract")

    def test_generate_text_rejects_developer_mapping_before_provider_call(self) -> None:
        client = self.make_client()
        fake_completions = _FakeCompletions(response=_completion("OK"))
        client.client = SimpleNamespace(chat=SimpleNamespace(completions=fake_completions))

        with self.assertRaisesRegex(ValueError, "Unsupported LLM message role"):
            asyncio.run(client.generate_text([{"role": "developer", "content": "developer contract"}]))

        self.assertEqual(fake_completions.calls, [])

    def test_provider_role_capabilities_reject_developer(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unsupported provider message roles: developer"):
            LLMClient(config=_base_config(provider_role_capabilities={"roles": ["system", "developer", "user"]}))

    def test_all_configured_model_editions_emit_only_four_role_agent_payloads(self) -> None:
        models = ("model-a", "model-b", "model-c", "model-d", "model-e")
        config = _base_config(models[0])
        config["model_editions"] = {
            "default": models[0],
            "options": [
                {
                    "value": model,
                    "label": model,
                    "reasoning_efforts": _reasoning_efforts(),
                    "agent_capabilities": {
                        "supports_messages": True,
                        "roles": ["system", "user", "assistant", "tool"],
                        "supports_native_tools": True,
                        "supports_required_tool_choice": True,
                        "supports_streamed_tool_calls": False,
                        "supports_non_stream_agent_sample": True,
                    },
                }
                for model in models
            ],
        }

        for model in models:
            with self.subTest(model=model):
                client = LLMClient(config=config, model=model)
                fake_completions = _FakeCompletions(response=_completion("OK"))
                client.client = SimpleNamespace(chat=SimpleNamespace(completions=fake_completions))
                request = AgentModelRequest(
                    "req",
                    AgentModelBinding(model),
                    (AgentMessage("system", "rules"), AgentMessage("user", "question")),
                )

                asyncio.run(client.generate_agent_sample(request))

                roles = {message["role"] for message in fake_completions.calls[0]["messages"]}
                self.assertLessEqual(roles, {"system", "assistant", "user", "tool"})
                self.assertNotIn("developer", roles)

    def test_generate_text_collapses_messages_to_single_user_block_when_messages_are_disabled(self) -> None:
        client = LLMClient(
            config=_base_config(provider_role_capabilities={"supports_messages": False, "roles": ["system", "user"]})
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
            config=_base_config(
                provider_feature_capabilities={
                    "supports_thinking": False,
                    "supports_reasoning_effort": False,
                }
            )
        )
        fake_completions = _FakeCompletions(response=_completion("OK"))
        client.client = SimpleNamespace(chat=SimpleNamespace(completions=fake_completions))

        asyncio.run(client.generate_text([LLMMessage(role="user", content="prompt")], thinking=False, reasoning_effort="minimal"))

        call = fake_completions.calls[0]
        self.assertNotIn("extra_body", call)
        self.assertNotIn("reasoning_effort", call)
        self.assertEqual(client.safe_metadata()["provider_feature_capabilities"]["supports_thinking"], False)

    def test_provider_cache_hint_defaults_disabled(self) -> None:
        client = self.make_client()
        fake_completions = _FakeCompletions(response=_completion("OK"))
        client.client = SimpleNamespace(chat=SimpleNamespace(completions=fake_completions))

        asyncio.run(client.generate_text("prompt", thinking=False))

        call = fake_completions.calls[0]
        self.assertEqual(call["extra_body"], {"thinking": {"type": "disabled"}})
        self.assertNotIn("prompt_cache", call["extra_body"])
        metadata = client.safe_metadata()
        self.assertEqual(metadata["provider_cache_capabilities"]["status"], "disabled")
        self.assertFalse(metadata["provider_cache_capabilities"]["prompt_cache_hint_enabled"])

    def test_provider_cache_hint_unsupported_provider_noops_when_enabled(self) -> None:
        client = LLMClient(
            config=_base_config(
                provider_cache_capabilities={
                    "supports_prompt_cache": False,
                    "prompt_cache_hint_enabled": True,
                    "prompt_cache_hint": {"type": "ephemeral"},
                }
            )
        )
        fake_completions = _FakeCompletions(response=_completion("OK"))
        client.client = SimpleNamespace(chat=SimpleNamespace(completions=fake_completions))

        asyncio.run(client.generate_text("prompt", thinking=False))

        call = fake_completions.calls[0]
        self.assertEqual(call["extra_body"], {"thinking": {"type": "disabled"}})
        self.assertEqual(client.last_provider_cache_hint_status["status"], "unsupported")
        metadata = client.safe_metadata()
        self.assertEqual(metadata["provider_cache_capabilities"]["status"], "unsupported")
        self.assertEqual(metadata["provider_cache_capabilities"]["hint_keys"], ["type"])
        self.assertNotIn("base_url", metadata)
        self.assertNotIn("api_key", metadata)

    def test_provider_cache_hint_supported_provider_adds_configured_hint(self) -> None:
        client = LLMClient(
            config=_base_config(
                provider_cache_capabilities={
                    "supports_prompt_cache": True,
                    "prompt_cache_hint_enabled": True,
                    "prompt_cache_hint": {"type": "ephemeral", "scope": "cacheable_prefix"},
                }
            )
        )
        fake_completions = _FakeCompletions(response=_completion("OK"))
        client.client = SimpleNamespace(chat=SimpleNamespace(completions=fake_completions))

        asyncio.run(client.generate_text("prompt", thinking=True, reasoning_effort="high"))

        call = fake_completions.calls[0]
        self.assertEqual(call["extra_body"]["thinking"], {"type": "enabled"})
        self.assertEqual(call["extra_body"]["prompt_cache"], {"type": "ephemeral", "scope": "cacheable_prefix"})
        self.assertEqual(client.last_provider_cache_hint_status["status"], "applied")
        metadata = client.safe_metadata()
        self.assertEqual(metadata["provider_cache_capabilities"]["status"], "applied")
        self.assertEqual(metadata["provider_cache_capabilities"]["hint_keys"], ["scope", "type"])

    def test_provider_cache_hint_supported_provider_also_applies_to_streaming(self) -> None:
        client = LLMClient(
            config=_base_config(
                provider_cache_capabilities={
                    "supports_prompt_cache": True,
                    "prompt_cache_hint_enabled": True,
                    "prompt_cache_hint": {"type": "ephemeral"},
                }
            )
        )
        fake_completions = _FakeCompletions([_chunk(answer="OK")])
        client.client = SimpleNamespace(chat=SimpleNamespace(completions=fake_completions))

        async def collect() -> list[dict[str, str | None]]:
            return [event async for event in client.generate_text_with_thinking("prompt", thinking=False)]

        self.assertEqual(asyncio.run(collect()), [{"answer": "OK", "reasoning": None}])
        self.assertEqual(fake_completions.calls[0]["extra_body"]["prompt_cache"], {"type": "ephemeral"})
        self.assertEqual(client.last_provider_cache_hint_status["status"], "applied")

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
