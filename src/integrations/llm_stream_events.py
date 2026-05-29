from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any


async def iter_stream_like(value: Any) -> AsyncIterator[Any]:
    if hasattr(value, "__aiter__"):
        async for item in value:
            yield item
        return
    if inspect.isawaitable(value):
        yield await value
        return
    if isinstance(value, str | Mapping):
        yield value
        return
    for item in value:
        yield item


def coerce_text_result(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, Mapping):
        for key in ("answer", "content", "delta", "text"):
            candidate = value.get(key)
            if candidate is not None:
                return str(candidate or "")
        return ""
    return str(value or "")


def coerce_stream_event(value: Any) -> dict[str, str | None] | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        answer = _optional_string(value.get("answer") if "answer" in value else value.get("delta"))
        reasoning = _optional_string(value.get("reasoning") if "reasoning" in value else value.get("reasoning_content"))
        if answer is None and reasoning is None:
            return None
        return {"answer": answer, "reasoning": reasoning}
    text = str(value)
    if not text:
        return None
    return {"answer": text, "reasoning": None}


def accepted_options(generator: Callable[..., Any], options: Mapping[str, Any]) -> dict[str, Any]:
    try:
        signature = inspect.signature(generator)
    except (TypeError, ValueError):
        return {}
    accepts_kwargs = any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in signature.parameters.values())
    return {key: value for key, value in options.items() if value is not None and (accepts_kwargs or key in signature.parameters)}


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None
