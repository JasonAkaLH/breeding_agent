from __future__ import annotations

import inspect
import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any


TextGenerator = Callable[..., str | Awaitable[str]]


class LLMOutputError(ValueError):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


async def call_text_generator(generator: TextGenerator, prompt: str, **options: Any) -> str:
    accepted_options = _accepted_options(generator, options)
    result = generator(prompt, **accepted_options)
    if inspect.isawaitable(result):
        result = await result
    return _coerce_text_result(result)


def _accepted_options(generator: TextGenerator, options: Mapping[str, Any]) -> dict[str, Any]:
    if not options:
        return {}
    try:
        signature = inspect.signature(generator)
    except (TypeError, ValueError):
        return {}
    accepts_kwargs = any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in signature.parameters.values())
    return {
        key: value
        for key, value in options.items()
        if value is not None and (accepts_kwargs or key in signature.parameters)
    }


def _coerce_text_result(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, Mapping):
        for key in ("answer", "content", "delta", "text"):
            candidate = value.get(key)
            if candidate is not None:
                return str(candidate or "")
        return ""
    return str(value or "")


def parse_json_object(text: str) -> dict[str, Any]:
    stripped = str(text or "").strip()
    if not stripped:
        raise LLMOutputError("empty_output", "LLM output is empty.")

    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", stripped, flags=re.S | re.I)
    if fenced:
        stripped = fenced.group(1).strip()

    if not stripped.startswith("{"):
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start >= 0 and end > start:
            stripped = stripped[start : end + 1]

    try:
        decoded = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise LLMOutputError("parse_failed", f"LLM output is not valid JSON: {exc.msg}") from exc

    if not isinstance(decoded, dict):
        raise LLMOutputError("parse_failed", "LLM JSON output must be an object.")
    return decoded


def json_ready(value: Any, *, max_string_length: int = 500) -> Any:
    if isinstance(value, Mapping):
        return {str(key): json_ready(item, max_string_length=max_string_length) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [json_ready(item, max_string_length=max_string_length) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        if isinstance(value, str) and len(value) > max_string_length:
            return value[:max_string_length] + "…"
        return value
    text = str(value)
    if len(text) > max_string_length:
        return text[:max_string_length] + "…"
    return text


def string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [str(item) for item in value]
    return [str(value)]
