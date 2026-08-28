from __future__ import annotations

import inspect
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from src.orchestration.agent_loop.models import (
    AgentFinishMetadata,
    AgentMessage,
    AgentModelContextLengthError,
    AgentModelRequest,
    AgentProtocolErrorCode,
    AgentProtocolFailure,
    AgentProtocolRetryPolicy,
    AgentProtocolViolation,
    AgentSample,
    AgentSamplingCancelled,
    AgentToolCall,
    AgentUsage,
    MODEL_MESSAGE_ROLES,
    canonical_json,
    validate_provider_safe_tool_name,
)


@dataclass(slots=True)
class _CallBuffer:
    index: int
    call_id: str = ""
    name: str = ""
    arguments: str = ""


@dataclass(slots=True)
class _ReasoningAttemptState:
    published: bool = False
    delivery_enabled: bool = True


_LOGGER = logging.getLogger(__name__)


class OpenAIAgentModelAdapter:
    def __init__(
        self,
        *,
        completions: Any,
        model: str,
        temperature: float = 0.0,
        retry_policy: AgentProtocolRetryPolicy | None = None,
        stream: bool = True,
        request_options: Mapping[str, Any] | None = None,
    ) -> None:
        self._completions = completions
        self._model = model
        self._temperature = temperature
        self._retry_policy = retry_policy or AgentProtocolRetryPolicy()
        self._stream = stream
        self._request_options = dict(request_options or {})

    async def sample_agent(self, request: AgentModelRequest) -> AgentSample:
        last_violation: AgentProtocolViolation | None = None
        for attempt in range(1, self._retry_policy.max_attempts + 1):
            self._raise_if_cancelled(request)
            reasoning_state = _ReasoningAttemptState()
            try:
                if self._stream:
                    return await self._sample_stream(
                        request,
                        attempt=attempt,
                        reasoning_state=reasoning_state,
                    )
                return await self._sample_non_stream(
                    request,
                    attempt=attempt,
                    reasoning_state=reasoning_state,
                )
            except AgentProtocolViolation as exc:
                await self._reset_reasoning(request, reasoning_state)
                last_violation = exc
            except Exception as exc:
                await self._reset_reasoning(request, reasoning_state)
                if _is_provider_context_length_error(exc):
                    raise AgentModelContextLengthError(
                        "agent_model_context_length_exceeded"
                    ) from exc
                raise
        assert last_violation is not None
        raise AgentProtocolFailure(last_violation.code, attempts=self._retry_policy.max_attempts)

    async def _sample_stream(
        self,
        request: AgentModelRequest,
        *,
        attempt: int,
        reasoning_state: _ReasoningAttemptState,
    ) -> AgentSample:
        stream = await self._completions.create(**self._request_payload(request, stream=True))
        text_parts: list[str] = []
        buffers: dict[int, _CallBuffer] = {}
        usage: Any = None
        finish_reason: str | None = None
        response_id: str | None = None
        try:
            async for chunk in stream:
                self._raise_if_cancelled(request)
                response_id = response_id or _field(chunk, "id")
                usage = _field(chunk, "usage") or usage
                choices = _field(chunk, "choices") or []
                if not choices:
                    continue
                choice = choices[0]
                finish_reason = _field(choice, "finish_reason") or finish_reason
                delta = _field(choice, "delta")
                if delta is None:
                    continue
                reasoning = _field(delta, "reasoning_content")
                await self._publish_reasoning(request, reasoning_state, reasoning)
                content = _field(delta, "content")
                if content:
                    text_parts.append(str(content))
                for raw_call in _field(delta, "tool_calls") or []:
                    index = _field(raw_call, "index")
                    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
                        raise AgentProtocolViolation(AgentProtocolErrorCode.INCOMPLETE_STREAM, "tool delta has invalid index")
                    buffer = buffers.setdefault(index, _CallBuffer(index=index))
                    call_id = _field(raw_call, "id")
                    if call_id:
                        fragment = str(call_id)
                        if buffer.call_id != fragment:
                            buffer.call_id += fragment
                    function = _field(raw_call, "function")
                    if function is not None:
                        buffer.name += str(_field(function, "name") or "")
                        buffer.arguments += str(_field(function, "arguments") or "")
        except Exception:
            await _close_stream(stream)
            raise
        if finish_reason is None:
            raise AgentProtocolViolation(AgentProtocolErrorCode.INCOMPLETE_STREAM, "stream ended without finish reason")
        return self._close_sample(
            request,
            attempt=attempt,
            sample_id=response_id or f"{request.request_id}:{attempt}",
            text="".join(text_parts),
            raw_calls=[buffers[index] for index in sorted(buffers)],
            usage=usage,
            finish_reason=finish_reason,
        )

    async def _sample_non_stream(
        self,
        request: AgentModelRequest,
        *,
        attempt: int,
        reasoning_state: _ReasoningAttemptState,
    ) -> AgentSample:
        response = await self._completions.create(**self._request_payload(request, stream=False))
        self._raise_if_cancelled(request)
        choices = _field(response, "choices") or []
        if not choices:
            raise AgentProtocolViolation(AgentProtocolErrorCode.EMPTY_SAMPLE, "response has no choices")
        choice = choices[0]
        message = _field(choice, "message")
        raw_calls: list[_CallBuffer] = []
        for index, raw_call in enumerate(_field(message, "tool_calls") or []):
            function = _field(raw_call, "function")
            raw_calls.append(
                _CallBuffer(
                    index=index,
                    call_id=str(_field(raw_call, "id") or ""),
                    name=str(_field(function, "name") or ""),
                    arguments=str(_field(function, "arguments") or ""),
                )
            )
        sample = self._close_sample(
            request,
            attempt=attempt,
            sample_id=str(_field(response, "id") or f"{request.request_id}:{attempt}"),
            text=str(_field(message, "content") or ""),
            raw_calls=raw_calls,
            usage=_field(response, "usage"),
            finish_reason=str(_field(choice, "finish_reason") or "stop"),
        )
        await self._publish_reasoning(
            request,
            reasoning_state,
            _field(message, "reasoning_content"),
        )
        return sample

    async def _publish_reasoning(
        self,
        request: AgentModelRequest,
        state: _ReasoningAttemptState,
        value: Any,
    ) -> None:
        sink = request.reasoning_delta_sink
        if (
            not request.binding.thinking_enabled
            or sink is None
            or not state.delivery_enabled
            or value is None
        ):
            return
        text = str(value)
        if not text.strip():
            return
        try:
            await sink(text)
        except Exception as exc:
            state.delivery_enabled = False
            _LOGGER.warning(
                "agent_reasoning_delta_delivery_failed",
                extra={"phase": "delta", "error_type": type(exc).__name__},
            )
            return
        state.published = True

    async def _reset_reasoning(
        self,
        request: AgentModelRequest,
        state: _ReasoningAttemptState,
    ) -> None:
        sink = request.reasoning_reset_sink
        if not state.published or sink is None:
            return
        try:
            await sink()
        except Exception as exc:
            _LOGGER.warning(
                "agent_reasoning_reset_delivery_failed",
                extra={"phase": "reset", "error_type": type(exc).__name__},
            )

    def _close_sample(
        self,
        request: AgentModelRequest,
        *,
        attempt: int,
        sample_id: str,
        text: str,
        raw_calls: list[_CallBuffer],
        usage: Any,
        finish_reason: str,
    ) -> AgentSample:
        calls: list[AgentToolCall] = []
        call_ids: set[str] = set()
        for ordinal, raw in enumerate(raw_calls):
            if not raw.call_id.strip():
                raise AgentProtocolViolation(AgentProtocolErrorCode.MISSING_CALL_ID, "tool call has no ID")
            if raw.call_id in call_ids:
                raise AgentProtocolViolation(AgentProtocolErrorCode.DUPLICATE_CALL_ID, "tool call ID is duplicated")
            call_ids.add(raw.call_id)
            try:
                validate_provider_safe_tool_name(raw.name)
            except ValueError as exc:
                raise AgentProtocolViolation(AgentProtocolErrorCode.INVALID_TOOL_NAME, str(exc)) from exc
            try:
                arguments = canonical_json(json.loads(raw.arguments))
            except (json.JSONDecodeError, ValueError, TypeError) as exc:
                raise AgentProtocolViolation(AgentProtocolErrorCode.MALFORMED_ARGUMENTS, "tool arguments are not valid JSON") from exc
            calls.append(
                AgentToolCall(
                    call_id=raw.call_id,
                    provider_safe_name=raw.name,
                    arguments_json=arguments,
                    ordinal=ordinal,
                )
            )
        self._validate_required_choice(request, calls)
        if not calls and not text.strip():
            raise AgentProtocolViolation(AgentProtocolErrorCode.EMPTY_SAMPLE, "sample has neither text nor tool calls")
        return AgentSample(
            sample_id=sample_id,
            binding=request.binding,
            visible_text=text,
            tool_calls=tuple(calls),
            usage=_usage(usage),
            finish=AgentFinishMetadata(
                finish_reason=finish_reason,
                attempts=attempt,
                mixed_text_and_tool_calls=bool(text and calls),
            ),
        )

    @staticmethod
    def _validate_required_choice(request: AgentModelRequest, calls: list[AgentToolCall]) -> None:
        if request.tool_choice.mode != "required":
            return
        if not calls:
            raise AgentProtocolViolation(AgentProtocolErrorCode.REQUIRED_TOOL_MISSING, "required tool was not called")
        if len(calls) != 1:
            raise AgentProtocolViolation(AgentProtocolErrorCode.REQUIRED_TOOL_MULTIPLE, "required choice produced multiple calls")
        if calls[0].provider_safe_name != request.tool_choice.required_name:
            raise AgentProtocolViolation(AgentProtocolErrorCode.REQUIRED_TOOL_MISMATCH, "required choice called a different tool")

    def _request_payload(self, request: AgentModelRequest, *, stream: bool) -> dict[str, Any]:
        payload: dict[str, Any] = dict(self._request_options)
        payload.update({
            "model": self._model,
            "messages": [_message_payload(message) for message in request.messages],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": tool.provider_safe_name,
                        "description": tool.description,
                        "parameters": dict(tool.input_schema),
                    },
                }
                for tool in request.tools
            ],
            "tool_choice": _tool_choice_payload(request),
            "stream": stream,
            "temperature": self._temperature,
        })
        if stream:
            payload["stream_options"] = {"include_usage": True}
        return payload

    @staticmethod
    def _raise_if_cancelled(request: AgentModelRequest) -> None:
        if request.cancellation is not None and request.cancellation.is_cancelled():
            raise AgentSamplingCancelled("Agent sampling was cancelled")


def _message_payload(message: AgentMessage) -> dict[str, Any]:
    if message.role not in MODEL_MESSAGE_ROLES:
        raise ValueError(f"Unsupported provider message role: {message.role}")
    payload: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.role == "tool":
        payload["tool_call_id"] = message.tool_call_id
    if message.tool_calls:
        payload["tool_calls"] = [
            {
                "id": call.call_id,
                "type": "function",
                "function": {"name": call.provider_safe_name, "arguments": call.arguments_json},
            }
            for call in message.tool_calls
        ]
    return payload


def _tool_choice_payload(request: AgentModelRequest) -> Any:
    if request.tool_choice.mode != "required":
        return request.tool_choice.mode
    return {
        "type": "function",
        "function": {"name": request.tool_choice.required_name},
    }


def _field(value: Any, name: str) -> Any:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _usage(value: Any) -> AgentUsage:
    if value is None:
        return AgentUsage()
    prompt = _field(value, "prompt_tokens")
    completion = _field(value, "completion_tokens")
    total = _field(value, "total_tokens")
    if prompt is None and completion is None and total is None:
        return AgentUsage()
    return AgentUsage(prompt_tokens=prompt, completion_tokens=completion, total_tokens=total, status="available")


async def _close_stream(stream: Any) -> None:
    close = getattr(stream, "aclose", None)
    if callable(close):
        result = close()
        if inspect.isawaitable(result):
            await result


def _is_provider_context_length_error(exc: BaseException) -> bool:
    response = _field(exc, "response")
    status = _field(exc, "status_code") or _field(response, "status_code")
    body = _field(exc, "body")
    error = body.get("error") if isinstance(body, Mapping) else None
    sources = tuple(
        value
        for value in (exc, body, error)
        if isinstance(value, Mapping) or value is exc
    )
    codes = {
        str(_field(source, "code") or "").strip().lower()
        for source in sources
    }
    types = {
        str(_field(source, "type") or "").strip().lower()
        for source in sources
    }
    return bool(
        status in {400, 413}
        and (
            codes
            & {
                "context_length_exceeded",
                "context_window_exceeded",
                "maximum_context_length_exceeded",
            }
            or types
            & {
                "context_length_exceeded",
                "context_window_exceeded",
            }
        )
    )
