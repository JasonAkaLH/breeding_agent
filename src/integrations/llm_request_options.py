from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .model_editions import ReasoningEffortConfig

ReasoningEffort = str


@dataclass(frozen=True, slots=True)
class LLMRequestOptions:
    thinking: bool
    reasoning_effort: ReasoningEffort
    model_edition: str | None = None
    requested_reasoning_effort: str | None = None


def resolve_llm_request_options(
    metadata: Mapping[str, Any] | None,
    *,
    fallback_reasoning_effort: ReasoningEffort | None = None,
    model_reasoning_configs: Mapping[str, ReasoningEffortConfig] | None = None,
    default_model_edition: str | None = None,
) -> LLMRequestOptions:
    values = metadata if isinstance(metadata, Mapping) else {}
    thinking = resolve_llm_thinking_enabled(values)
    model_edition = resolve_llm_model_edition(values) or default_model_edition
    requested = _explicit_reasoning_effort(values)
    return LLMRequestOptions(
        thinking=thinking,
        reasoning_effort=resolve_llm_reasoning_effort(
            values,
            fallback=fallback_reasoning_effort,
            thinking_enabled=thinking,
            model_edition=model_edition,
            model_reasoning_configs=model_reasoning_configs,
        ),
        model_edition=model_edition,
        requested_reasoning_effort=requested,
    )


def resolve_llm_reasoning_effort(
    metadata: Mapping[str, Any] | None,
    *,
    fallback: ReasoningEffort | None,
    thinking_enabled: bool,
    model_edition: str | None = None,
    model_reasoning_configs: Mapping[str, ReasoningEffortConfig] | None = None,
) -> ReasoningEffort:
    values = metadata if isinstance(metadata, Mapping) else {}
    explicit = _explicit_reasoning_effort(values)
    cfg = _selected_reasoning_config(
        model_edition=model_edition or resolve_llm_model_edition(values),
        model_reasoning_configs=model_reasoning_configs,
    )
    if cfg is None:
        if explicit:
            return explicit
        if fallback:
            return fallback
        raise ValueError("No model reasoning_efforts config is available")

    state_name = "enabled" if thinking_enabled else "disabled"
    policy = cfg.policy_for(thinking_enabled)
    if not policy.supported:
        raise ValueError(f"Model {model_edition or '<default>'} does not support thinking={state_name}")
    candidate = explicit or policy.default
    if not candidate:
        raise ValueError(f"Model {model_edition or '<default>'} is missing thinking={state_name} default")
    if candidate not in cfg.option_values():
        raise ValueError(f"Unknown reasoning_effort for model {model_edition or '<default>'}: {candidate}")
    if not cfg.supports(candidate, thinking_enabled=thinking_enabled):
        raise ValueError(
            f"Model {model_edition or '<default>'} does not support "
            f"reasoning_effort={candidate} when thinking={state_name}"
        )
    return candidate


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


def _explicit_reasoning_effort(metadata: Mapping[str, Any]) -> str | None:
    explicit = metadata.get("main_agent_reasoning_effort")
    if isinstance(explicit, str):
        cleaned = explicit.strip()
        return cleaned or None
    return None


def _selected_reasoning_config(
    *,
    model_edition: str | None,
    model_reasoning_configs: Mapping[str, ReasoningEffortConfig] | None,
) -> ReasoningEffortConfig | None:
    if not model_reasoning_configs:
        return None
    if model_edition:
        cfg = model_reasoning_configs.get(model_edition)
        if cfg is None:
            raise ValueError(f"Unsupported model_edition: {model_edition}")
        return cfg
    if len(model_reasoning_configs) == 1:
        return next(iter(model_reasoning_configs.values()))
    raise ValueError("model_edition is required when multiple model reasoning configs are available")
