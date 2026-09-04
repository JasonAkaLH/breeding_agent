from __future__ import annotations

import hashlib
import inspect
import json
from collections.abc import AsyncIterator, Mapping
from typing import Any, Callable

from src.orchestration.agent_loop.models import (
    AgentFinishMetadata,
    AgentModelRequest,
    AgentSample,
    AgentToolCall,
    AgentUsage,
)

from .model_errors import raise_for_model_unavailable


class StreamAgentModelAdapter:
    """Adapt an explicitly injected text stream fixture to AgentModelPort."""

    def __init__(self, generator: Callable[..., Any]) -> None:
        self._generator = generator

    async def sample_agent(self, request: AgentModelRequest) -> AgentSample:
        sample_id = "sample-" + hashlib.sha256(
            request.request_id.encode("utf-8")
        ).hexdigest()[:20]
        if request.tool_choice.mode == "required":
            tool = next(
                item
                for item in request.tools
                if item.provider_safe_name == request.tool_choice.required_name
            )
            arguments = _required_arguments(tool.input_schema, request)
            return AgentSample(
                sample_id=sample_id,
                binding=request.binding,
                visible_text="",
                tool_calls=(
                    AgentToolCall(
                        call_id=f"call-{sample_id}",
                        provider_safe_name=tool.provider_safe_name,
                        arguments_json=json.dumps(
                            arguments,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        ordinal=0,
                    ),
                ),
                usage=AgentUsage(status="usage_unavailable"),
                finish=AgentFinishMetadata("tool_calls", 1),
            )
        prompt = "\n".join(
            message.content
            for message in request.messages
            if isinstance(message.content, str) and message.content
        )
        options = {
            "thinking": request.binding.thinking_enabled,
            "reasoning_effort": request.binding.reasoning_effort,
            "model_edition": request.binding.model_edition,
        }
        try:
            signature = inspect.signature(self._generator)
            accepts_kwargs = any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in signature.parameters.values()
            )
            accepted = {
                key: value
                for key, value in options.items()
                if accepts_kwargs or key in signature.parameters
            }
        except (TypeError, ValueError):
            accepted = {}
        try:
            value = self._generator(prompt, **accepted)
            if inspect.isawaitable(value):
                value = await value
            text = await _collect_text(
                value,
                on_reasoning_delta=request.reasoning_delta_sink,
            )
        except Exception as exc:
            raise_for_model_unavailable(exc)
            raise
        tool_calls = _tool_calls_from_fixture(text, request)
        if tool_calls:
            return AgentSample(
                sample_id=sample_id,
                binding=request.binding,
                visible_text="",
                tool_calls=tool_calls,
                usage=AgentUsage(status="usage_unavailable"),
                finish=AgentFinishMetadata("tool_calls", 1),
            )
        return AgentSample(
            sample_id=sample_id,
            binding=request.binding,
            visible_text=text or "测试回答",
            tool_calls=(),
            usage=AgentUsage(status="usage_unavailable"),
            finish=AgentFinishMetadata("stop", 1),
        )


def _tool_calls_from_fixture(
    text: str,
    request: AgentModelRequest,
) -> tuple[AgentToolCall, ...]:
    try:
        payload = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return ()
    if not isinstance(payload, Mapping) or not isinstance(payload.get("tool_calls"), list):
        return ()
    tools_by_capability = {tool.capability_id: tool for tool in request.tools}
    tools_by_name = {tool.provider_safe_name: tool for tool in request.tools}
    calls: list[AgentToolCall] = []
    for ordinal, raw in enumerate(payload["tool_calls"]):
        if not isinstance(raw, Mapping):
            return ()
        tool = tools_by_capability.get(str(raw.get("capability_id") or ""))
        if tool is None:
            tool = tools_by_name.get(str(raw.get("provider_safe_name") or ""))
        arguments = raw.get("arguments", {})
        if tool is None or not isinstance(arguments, Mapping):
            return ()
        calls.append(
            AgentToolCall(
                call_id=str(raw.get("call_id") or f"call-{sample_id_part(request)}-{ordinal}"),
                provider_safe_name=tool.provider_safe_name,
                arguments_json=json.dumps(
                    dict(arguments),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                ordinal=ordinal,
            )
        )
    return tuple(calls)


def sample_id_part(request: AgentModelRequest) -> str:
    return hashlib.sha256(request.request_id.encode("utf-8")).hexdigest()[:12]


def _required_arguments(
    schema: Mapping[str, Any], request: AgentModelRequest
) -> dict[str, Any]:
    properties = schema.get("properties")
    values: dict[str, Any] = {}
    if not isinstance(properties, Mapping):
        return values
    required = schema.get("required")
    required_names = required if isinstance(required, list | tuple) else ()
    current_user = next(
        (
            message.content
            for message in reversed(request.messages)
            if message.role == "user" and isinstance(message.content, str)
        ),
        "",
    )
    for name in required_names:
        field = properties.get(name)
        if not isinstance(name, str) or not isinstance(field, Mapping):
            continue
        enum = field.get("enum")
        if isinstance(enum, list | tuple) and enum:
            values[name] = enum[0]
        elif field.get("type") == "string" and name == "query":
            values[name] = current_user
    return values


async def _collect_text(value: Any, *, on_reasoning_delta=None) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, AsyncIterator) or hasattr(value, "__aiter__"):
        chunks: list[str] = []
        async for event in value:
            if isinstance(event, Mapping):
                reasoning = event.get("reasoning")
                if (
                    isinstance(reasoning, str)
                    and reasoning
                    and on_reasoning_delta is not None
                ):
                    await on_reasoning_delta(reasoning)
                answer = event.get("answer")
                if isinstance(answer, str):
                    chunks.append(answer)
            elif isinstance(event, str):
                chunks.append(event)
        return "".join(chunks)
    return str(value or "")
