from __future__ import annotations

import unittest
from types import SimpleNamespace

from src.integrations.openai_agent_model_adapter import OpenAIAgentModelAdapter
from src.orchestration.agent_loop.models import (
    AgentCancellationToken,
    AgentMessage,
    AgentModelBinding,
    AgentModelRequest,
    AgentProtocolErrorCode,
    AgentProtocolFailure,
    AgentProtocolRetryPolicy,
    AgentSamplingCancelled,
    AgentToolChoice,
    AgentToolDescriptor,
)


def _ns(**kwargs):
    return SimpleNamespace(**kwargs)


def _delta_call(index: int, *, call_id: str | None = None, name: str | None = None, arguments: str | None = None):
    return _ns(index=index, id=call_id, function=_ns(name=name, arguments=arguments))


def _chunk(*, text: str | None = None, calls: list[object] | None = None, finish: str | None = None, usage=None, response_id="sample-1"):
    return _ns(
        id=response_id,
        choices=[_ns(delta=_ns(content=text, tool_calls=calls or []), finish_reason=finish)],
        usage=usage,
    )


class _Stream:
    def __init__(self, chunks, *, cancel: AgentCancellationToken | None = None, cancel_after: int | None = None):
        self._chunks = iter(chunks)
        self._cancel = cancel
        self._cancel_after = cancel_after
        self._count = 0
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            chunk = next(self._chunks)
        except StopIteration as exc:
            raise StopAsyncIteration from exc
        self._count += 1
        if self._cancel is not None and self._cancel_after == self._count:
            self._cancel.cancel()
        return chunk

    async def aclose(self):
        self.closed = True


class _Completions:
    def __init__(self, responses):
        self._responses = iter(responses)
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return next(self._responses)


def _binding() -> AgentModelBinding:
    return AgentModelBinding("edition-a", reasoning_effort="high", thinking_enabled=True, option_digests={"policy": "abc"})


def _tool(capability_id="skill.weather", safe_name="weather_0123456789ab") -> AgentToolDescriptor:
    return AgentToolDescriptor(
        provider_safe_name=safe_name,
        capability_id=capability_id,
        description="weather",
        input_schema={"type": "object", "properties": {"city": {"type": "string"}}},
    )


def _request(*, choice=None, cancellation=None, tools=None) -> AgentModelRequest:
    return AgentModelRequest(
        request_id="req-1",
        binding=_binding(),
        messages=(
            AgentMessage("system", "rules"),
            AgentMessage("assistant", "prior"),
            AgentMessage("tool", "observation", tool_call_id="old-call"),
            AgentMessage("user", "question"),
        ),
        tools=tuple(tools or (_tool(),)),
        tool_choice=choice or AgentToolChoice("auto"),
        cancellation=cancellation,
    )


class OpenAIAgentModelAdapterTest(unittest.IsolatedAsyncioTestCase):
    async def test_agent_message_rejects_developer_role(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unsupported Agent message role"):
            AgentMessage("developer", "legacy contract")

    async def test_provider_payload_revalidates_message_role(self) -> None:
        message = AgentMessage("user", "question")
        object.__setattr__(message, "role", "developer")
        request = AgentModelRequest("req", _binding(), (message,))
        completions = _Completions([])

        with self.assertRaisesRegex(ValueError, "Unsupported provider message role"):
            await OpenAIAgentModelAdapter(
                completions=completions,
                model="edition-a",
                retry_policy=AgentProtocolRetryPolicy(0),
                stream=False,
            ).sample_agent(request)

        self.assertEqual(completions.calls, [])

    async def test_non_stream_fallback_closes_native_tool_sample(self) -> None:
        response = _ns(
            id="sample-non-stream",
            choices=[
                _ns(
                    finish_reason="tool_calls",
                    message=_ns(
                        content=None,
                        tool_calls=[
                            _ns(
                                id="c1",
                                function=_ns(name="weather_0123456789ab", arguments='{"city":"北京"}'),
                            )
                        ],
                    ),
                )
            ],
            usage=_ns(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )
        completions = _Completions([response])
        sample = await OpenAIAgentModelAdapter(
            completions=completions,
            model="edition-a",
            retry_policy=AgentProtocolRetryPolicy(0),
            stream=False,
        ).sample_agent(_request())
        self.assertEqual(sample.tool_calls[0].arguments_json, '{"city":"北京"}')
        self.assertEqual(sample.usage.total_tokens, 15)
        self.assertEqual(sample.usage.status, "available")
        self.assertFalse(completions.calls[0]["stream"])

    async def test_transport_error_is_not_protocol_retried(self) -> None:
        class TransportFailureCompletions:
            def __init__(self):
                self.calls = 0

            async def create(self, **_kwargs):
                self.calls += 1
                raise ConnectionError("provider unavailable")

        completions = TransportFailureCompletions()
        adapter = OpenAIAgentModelAdapter(completions=completions, model="edition-a")
        with self.assertRaisesRegex(ConnectionError, "provider unavailable"):
            await adapter.sample_agent(_request())
        self.assertEqual(completions.calls, 1)

    async def test_closes_zero_one_and_multiple_calls_in_order(self) -> None:
        cases = [
            ([_chunk(text="answer", finish="stop")], 0, True),
            ([_chunk(calls=[_delta_call(0, call_id="c1", name="weather_0123456789ab", arguments='{"city":"上海"}')], finish="tool_calls")], 1, False),
            (
                [
                    _chunk(calls=[_delta_call(1, call_id="c2", name="unknown_0123456789ab", arguments="{}")]),
                    _chunk(calls=[_delta_call(0, call_id="c1", name="weather_0123456789ab", arguments="{}")], finish="tool_calls"),
                ],
                2,
                False,
            ),
        ]
        for chunks, expected_count, final_candidate in cases:
            completions = _Completions([_Stream(chunks)])
            sample = await OpenAIAgentModelAdapter(
                completions=completions,
                model="edition-a",
                retry_policy=AgentProtocolRetryPolicy(0),
            ).sample_agent(_request())
            self.assertEqual(len(sample.tool_calls), expected_count)
            self.assertEqual(sample.is_final_candidate, final_candidate)
            self.assertEqual([call.ordinal for call in sample.tool_calls], list(range(expected_count)))

    async def test_assembles_interleaved_fragmented_tool_deltas(self) -> None:
        stream = _Stream(
            [
                _chunk(calls=[_delta_call(0, call_id="call_", name="weather_", arguments='{"ci'), _delta_call(1, call_id="c2", name="unknown_", arguments="{")]),
                _chunk(calls=[_delta_call(1, name="0123456789ab", arguments="}"), _delta_call(0, call_id="123", name="0123456789ab", arguments='ty":"上海"}')], finish="tool_calls"),
            ]
        )
        sample = await OpenAIAgentModelAdapter(
            completions=_Completions([stream]), model="edition-a", retry_policy=AgentProtocolRetryPolicy(0)
        ).sample_agent(_request())

        self.assertEqual(sample.tool_calls[0].call_id, "call_123")
        self.assertEqual(sample.tool_calls[0].provider_safe_name, "weather_0123456789ab")
        self.assertEqual(sample.tool_calls[0].arguments_json, '{"city":"上海"}')
        self.assertEqual(sample.tool_calls[1].provider_safe_name, "unknown_0123456789ab")

    async def test_protocol_violations_retry_then_fail_with_closed_code(self) -> None:
        invalid_streams = {
            AgentProtocolErrorCode.MISSING_CALL_ID: [_chunk(calls=[_delta_call(0, name="weather_0123456789ab", arguments="{}")], finish="tool_calls")],
            AgentProtocolErrorCode.INVALID_TOOL_NAME: [_chunk(calls=[_delta_call(0, call_id="c1", name="bad name", arguments="{}")], finish="tool_calls")],
            AgentProtocolErrorCode.MALFORMED_ARGUMENTS: [_chunk(calls=[_delta_call(0, call_id="c1", name="weather_0123456789ab", arguments="{")], finish="tool_calls")],
            AgentProtocolErrorCode.DUPLICATE_CALL_ID: [_chunk(calls=[_delta_call(0, call_id="c1", name="weather_0123456789ab", arguments="{}"), _delta_call(1, call_id="c1", name="unknown_0123456789ab", arguments="{}")], finish="tool_calls")],
            AgentProtocolErrorCode.INCOMPLETE_STREAM: [_chunk(text="partial")],
        }
        for expected_code, chunks in invalid_streams.items():
            completions = _Completions([_Stream(chunks), _Stream(chunks)])
            adapter = OpenAIAgentModelAdapter(completions=completions, model="edition-a")
            with self.assertRaises(AgentProtocolFailure) as raised:
                await adapter.sample_agent(_request())
            self.assertEqual(raised.exception.code, expected_code)
            self.assertEqual(raised.exception.attempts, 2)
            self.assertEqual(len(completions.calls), 2)

    async def test_unknown_but_legal_tool_name_is_preserved(self) -> None:
        sample = await OpenAIAgentModelAdapter(
            completions=_Completions([_Stream([_chunk(calls=[_delta_call(0, call_id="c1", name="not_in_catalog", arguments="{}")], finish="tool_calls")])]),
            model="edition-a",
            retry_policy=AgentProtocolRetryPolicy(0),
        ).sample_agent(_request())
        self.assertEqual(sample.tool_calls[0].provider_safe_name, "not_in_catalog")

    async def test_required_choice_rejects_zero_multiple_and_wrong_calls(self) -> None:
        required = AgentToolChoice("required", "weather_0123456789ab")
        invalid = [
            ([_chunk(text="fallback", finish="stop")], AgentProtocolErrorCode.REQUIRED_TOOL_MISSING),
            ([_chunk(calls=[_delta_call(0, call_id="c1", name="weather_0123456789ab", arguments="{}"), _delta_call(1, call_id="c2", name="weather_0123456789ab", arguments="{}")], finish="tool_calls")], AgentProtocolErrorCode.REQUIRED_TOOL_MULTIPLE),
            ([_chunk(calls=[_delta_call(0, call_id="c1", name="other_tool", arguments="{}")], finish="tool_calls")], AgentProtocolErrorCode.REQUIRED_TOOL_MISMATCH),
        ]
        for chunks, expected in invalid:
            adapter = OpenAIAgentModelAdapter(
                completions=_Completions([_Stream(chunks)]), model="edition-a", retry_policy=AgentProtocolRetryPolicy(0)
            )
            with self.assertRaises(AgentProtocolFailure) as raised:
                await adapter.sample_agent(_request(choice=required))
            self.assertEqual(raised.exception.code, expected)

    async def test_mixed_text_and_calls_is_not_final_and_usage_can_be_unavailable(self) -> None:
        completions = _Completions([_Stream([_chunk(text="audit text", calls=[_delta_call(0, call_id="c1", name="weather_0123456789ab", arguments="{}")], finish="tool_calls")])])
        sample = await OpenAIAgentModelAdapter(
            completions=completions, model="edition-a", retry_policy=AgentProtocolRetryPolicy(0)
        ).sample_agent(_request())
        self.assertEqual(sample.visible_text, "audit text")
        self.assertTrue(sample.finish.mixed_text_and_tool_calls)
        self.assertFalse(sample.is_final_candidate)
        self.assertEqual(sample.usage.status, "usage_unavailable")

    async def test_cancellation_closes_stream_and_emits_no_partial_sample(self) -> None:
        token = AgentCancellationToken()
        stream = _Stream([_chunk(text="partial"), _chunk(text="ignored", finish="stop")], cancel=token, cancel_after=1)
        adapter = OpenAIAgentModelAdapter(completions=_Completions([stream]), model="edition-a")
        with self.assertRaises(AgentSamplingCancelled):
            await adapter.sample_agent(_request(cancellation=token))
        self.assertTrue(stream.closed)

    async def test_wire_uses_native_roles_tools_and_named_required_choice_without_fallback(self) -> None:
        completions = _Completions([_Stream([_chunk(calls=[_delta_call(0, call_id="c1", name="weather_0123456789ab", arguments="{}")], finish="tool_calls")])])
        await OpenAIAgentModelAdapter(
            completions=completions, model="edition-a", retry_policy=AgentProtocolRetryPolicy(0)
        ).sample_agent(_request(choice=AgentToolChoice("required", "weather_0123456789ab")))
        call = completions.calls[0]
        self.assertEqual([message["role"] for message in call["messages"]], ["system", "assistant", "tool", "user"])
        self.assertEqual(call["messages"][2]["tool_call_id"], "old-call")
        self.assertEqual(call["tools"][0]["type"], "function")
        self.assertEqual(call["tool_choice"], {"type": "function", "function": {"name": "weather_0123456789ab"}})

    def test_binding_safe_serialization_excludes_provider_objects_and_credentials(self) -> None:
        payload = _binding().to_safe_dict()
        serialized = str(payload)
        self.assertEqual(payload["model_edition"], "edition-a")
        for forbidden in ("client", "api_key", "base_url", "provider"):
            self.assertNotIn(forbidden, serialized)

    def test_provider_safe_mapping_is_stable_and_collision_resistant(self) -> None:
        first = AgentToolDescriptor.for_capability("skill.weather.v1", description="weather", input_schema={})
        repeated = AgentToolDescriptor.for_capability("skill.weather.v1", description="weather", input_schema={})
        colliding_slug = AgentToolDescriptor.for_capability("skill/weather/v1", description="weather", input_schema={})
        self.assertEqual(first.provider_safe_name, repeated.provider_safe_name)
        self.assertNotEqual(first.provider_safe_name, colliding_slug.provider_safe_name)
        self.assertLessEqual(len(first.provider_safe_name), 64)


class AgentProtocolRetryPolicyTest(unittest.TestCase):
    def test_default_and_validation_are_separate_from_transport_retries(self) -> None:
        self.assertEqual(AgentProtocolRetryPolicy.from_config({"max_retries": 99}).max_attempts, 2)
        self.assertEqual(AgentProtocolRetryPolicy.from_config({"agent_protocol_max_retries": 0}).max_attempts, 1)
        with self.assertRaisesRegex(ValueError, "non-negative integer"):
            AgentProtocolRetryPolicy.from_config({"agent_protocol_max_retries": -1})
