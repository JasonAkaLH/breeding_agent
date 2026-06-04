from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping

ReasoningEffort = Literal["minimal", "high", "max"]

_REASONING_EFFORTS: set[str] = {"minimal", "high", "max"}


@dataclass(frozen=True, slots=True)
class LLMRequestOptions:
    thinking: bool
    reasoning_effort: ReasoningEffort
    model_edition: str | None = None


def resolve_llm_request_options(
    metadata: Mapping[str, Any] | None,
    *,
    fallback_reasoning_effort: ReasoningEffort = "minimal",
) -> LLMRequestOptions:
    values = metadata if isinstance(metadata, Mapping) else {}
    thinking = resolve_llm_thinking_enabled(values)
    return LLMRequestOptions(
        thinking=thinking,
        reasoning_effort=resolve_llm_reasoning_effort(
            values,
            fallback=fallback_reasoning_effort,
            thinking_enabled=thinking,
        ),
        model_edition=resolve_llm_model_edition(values),
    )


def resolve_llm_reasoning_effort(
    metadata: Mapping[str, Any] | None,
    *,
    fallback: ReasoningEffort,
    thinking_enabled: bool,
) -> ReasoningEffort:
    if not thinking_enabled:
        return "minimal"
    values = metadata if isinstance(metadata, Mapping) else {}
    explicit = values.get("main_agent_reasoning_effort")
    if isinstance(explicit, str) and explicit in _REASONING_EFFORTS:
        return explicit  # type: ignore[return-value]
    return fallback


def resolve_llm_thinking_enabled(metadata: Mapping[str, Any] | None) -> bool:
    values = metadata if isinstance(metadata, Mapping) else {}
    if "main_agent_thinking_enabled" in values:
        return _is_truthy(values.get("main_agent_thinking_enabled"))
    return _is_truthy(values.get("deep_thinking"))


def resolve_llm_model_edition(metadata: Mapping[str, Any] | None) -> str | None:
    values = metadata if isinstance(metadata, Mapping) else {}
    value = values.get("model_edition")
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    return None


def llm_option_metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    values = metadata if isinstance(metadata, Mapping) else {}
    allowed = (
        "deep_thinking",
        "main_agent_thinking_enabled",
        "main_agent_reasoning_effort",
        "model_edition",
        "skill_input_trim_max_tokens",
        "soft_skill_trim_max_tokens",
        "trim_max_tokens",
    )
    return {key: values[key] for key in allowed if key in values}


def _is_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}
    return bool(value)
