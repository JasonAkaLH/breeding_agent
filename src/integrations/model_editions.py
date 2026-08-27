from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from src.core.coercion import coerce_positive_int, coerce_truthy
from src.orchestration.agent_loop.models import MODEL_MESSAGE_ROLES


@dataclass(frozen=True, slots=True)
class ReasoningEffortOption:
    value: str
    label: str
    allow_when_thinking_disabled: bool = False


@dataclass(frozen=True, slots=True)
class ReasoningEffortConfig:
    default: str
    disabled_default: str | None
    options: tuple[ReasoningEffortOption, ...]

    def option_values(self) -> tuple[str, ...]:
        return tuple(option.value for option in self.options)

    def disabled_safe_values(self) -> tuple[str, ...]:
        return tuple(option.value for option in self.options if option.allow_when_thinking_disabled)

    def has_value(self, value: str) -> bool:
        return value in self.option_values()

    def allows_when_thinking_disabled(self, value: str) -> bool:
        return value in self.disabled_safe_values()


@dataclass(frozen=True, slots=True)
class ModelEditionOption:
    value: str
    label: str
    trim_max_tokens: int | None = None
    reasoning_efforts: ReasoningEffortConfig | None = None
    agent_capabilities: AgentModelCapabilities | None = None


@dataclass(frozen=True, slots=True)
class AgentModelCapabilities:
    supports_messages: bool
    roles: frozenset[str]
    supports_native_tools: bool
    supports_required_tool_choice: bool
    supports_streamed_tool_calls: bool
    supports_non_stream_agent_sample: bool = False

    def missing_requirements(self) -> tuple[str, ...]:
        missing: list[str] = []
        if not self.supports_messages:
            missing.append("messages")
        absent_roles = sorted(MODEL_MESSAGE_ROLES - self.roles)
        if absent_roles:
            missing.append("roles=" + ",".join(absent_roles))
        unsupported_roles = sorted(self.roles - MODEL_MESSAGE_ROLES)
        if unsupported_roles:
            missing.append("roles=unsupported:" + ",".join(unsupported_roles))
        if not self.supports_native_tools:
            missing.append("native_tools")
        if not self.supports_required_tool_choice:
            missing.append("required_tool_choice")
        if not (self.supports_streamed_tool_calls or self.supports_non_stream_agent_sample):
            missing.append("streamed_tool_calls_or_non_stream_fallback")
        return tuple(missing)

    @property
    def agent_ready(self) -> bool:
        return not self.missing_requirements()


_MODEL_EDITION_CONTAINER_KEYS = (
    "model_editions",
    "model_edition_options",
    "allowed_model_editions",
)
_MODEL_EDITION_DEFAULT_KEYS = (
    "default_model_edition",
    "model_edition_default",
)


def model_edition_options(config: Mapping[str, Any] | None = None) -> tuple[ModelEditionOption, ...]:
    """Return selectable model editions declared by deployment config.

    Supported config forms:
    - model_editions: {default: <value>, options: [{value, label}, ...]}
    - model_editions: [<value>, {value, label}, ...]
    - model_edition_options / allowed_model_editions with the same list forms

    Legacy top-level ``model_edition`` / ``model`` remains a single-option
    fallback for callers that only need model labels. Product/runtime startup
    validation still requires every resolved option to declare
    ``reasoning_efforts``.
    """

    config = config or {}
    for key in _MODEL_EDITION_CONTAINER_KEYS:
        parsed = _parse_options_container(config.get(key))
        if parsed:
            return parsed

    legacy = _clean_text(config.get("model_edition") or config.get("model"))
    if legacy:
        return (
            ModelEditionOption(
                value=legacy,
                label=legacy,
                trim_max_tokens=coerce_positive_int(config.get("trim_max_tokens")),
            ),
        )
    return ()


def configured_model_editions(config: Mapping[str, Any] | None = None) -> tuple[str, ...]:
    return tuple(option.value for option in model_edition_options(config))


def model_reasoning_effort_configs(
    config: Mapping[str, Any] | None = None,
    *,
    validate: bool = True,
) -> dict[str, ReasoningEffortConfig]:
    options = model_edition_options(config)
    if validate:
        validate_model_reasoning_effort_configs(config)
    return {
        option.value: option.reasoning_efforts
        for option in options
        if option.reasoning_efforts is not None
    }


def model_reasoning_effort_config(
    model_edition: str | None,
    *,
    config: Mapping[str, Any] | None = None,
) -> ReasoningEffortConfig | None:
    selected = _clean_text(model_edition) or default_model_edition(config)
    if not selected:
        return None
    return model_reasoning_effort_configs(config).get(selected)


def validate_model_reasoning_effort_configs(config: Mapping[str, Any] | None = None) -> None:
    config = config or {}
    options = model_edition_options(config)
    if not options:
        return
    errors: list[str] = []
    for option in options:
        cfg = option.reasoning_efforts
        if cfg is None:
            errors.append(f"{option.value}: missing reasoning_efforts")
            continue
        values = cfg.option_values()
        if not values:
            errors.append(f"{option.value}: reasoning_efforts.options must not be empty")
            continue
        if len(set(values)) != len(values):
            errors.append(f"{option.value}: duplicate reasoning_efforts option value")
        if not cfg.default or cfg.default not in values:
            errors.append(f"{option.value}: reasoning_efforts.default must reference an option")
        disabled_safe = set(cfg.disabled_safe_values())
        if disabled_safe:
            if not cfg.disabled_default or cfg.disabled_default not in disabled_safe:
                errors.append(
                    f"{option.value}: reasoning_efforts.disabled_default must reference a disabled-safe option"
                )
        elif cfg.disabled_default:
            errors.append(f"{option.value}: disabled_default is only allowed when a disabled-safe option exists")
        for effort in cfg.options:
            if not effort.value:
                errors.append(f"{option.value}: reasoning_efforts option value must not be empty")
    if errors:
        raise ValueError("Invalid model reasoning_efforts config: " + "; ".join(errors))


def default_model_edition(config: Mapping[str, Any] | None = None) -> str | None:
    config = config or {}
    options = model_edition_options(config)
    allowed = {option.value for option in options}
    for key in _MODEL_EDITION_DEFAULT_KEYS:
        candidate = _clean_text(config.get(key))
        if candidate and (not allowed or candidate in allowed):
            return candidate

    container_default = _container_default(config.get("model_editions"))
    if container_default and (not allowed or container_default in allowed):
        return container_default

    legacy = _clean_text(config.get("model_edition") or config.get("model"))
    if legacy and (not allowed or legacy in allowed):
        return legacy
    if options:
        return options[0].value
    return None


def validate_model_edition(value: str | None, *, config: Mapping[str, Any] | None = None) -> str | None:
    if value is None:
        return None
    model_edition = value.strip()
    if not model_edition:
        return None
    allowed = configured_model_editions(config)
    if not allowed:
        raise ValueError("model_edition was provided but no model editions are configured")
    if model_edition not in allowed:
        raise ValueError(f"Unsupported model_edition: {model_edition}")
    return model_edition


def config_with_model_edition(config: Mapping[str, Any], model_edition: str) -> dict[str, Any]:
    next_config = dict(config)
    next_config["model_edition"] = model_edition
    next_config["_selected_model_edition"] = model_edition
    trim_max_tokens = trim_max_tokens_for_model_edition(model_edition, config=config)
    if trim_max_tokens is not None:
        next_config["trim_max_tokens"] = trim_max_tokens
    elif model_edition_options(config):
        next_config.pop("trim_max_tokens", None)
    return next_config


def trim_max_tokens_for_model_edition(model_edition: str | None, *, config: Mapping[str, Any] | None = None) -> int | None:
    config = config or {}
    selected = _clean_text(model_edition) or default_model_edition(config)
    options = model_edition_options(config)
    if selected:
        for option in options:
            if option.value == selected:
                return option.trim_max_tokens
    if options:
        return None
    return coerce_positive_int(config.get("trim_max_tokens"))


def config_for_model_edition(config: Mapping[str, Any] | None, model_edition: str | None) -> dict[str, Any]:
    base = dict(config or {})
    selected = _clean_text(model_edition) or default_model_edition(base)
    if selected:
        return config_with_model_edition(base, selected)
    trim_max_tokens = trim_max_tokens_for_model_edition(None, config=base)
    if trim_max_tokens is not None:
        base["trim_max_tokens"] = trim_max_tokens
    return base


def _parse_options_container(value: Any) -> tuple[ModelEditionOption, ...]:
    if isinstance(value, Mapping):
        nested = value.get("options") or value.get("values") or value.get("allowed") or value.get("editions")
        return _parse_options_list(nested)
    return _parse_options_list(value)


def _parse_options_list(value: Any) -> tuple[ModelEditionOption, ...]:
    if isinstance(value, str):
        candidates: Sequence[Any] = value.replace("\n", ",").split(",")
    elif isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        candidates = value
    else:
        return ()

    parsed: list[ModelEditionOption] = []
    seen: set[str] = set()
    for candidate in candidates:
        option = _parse_option(candidate)
        if option is None or option.value in seen:
            continue
        parsed.append(option)
        seen.add(option.value)
    return tuple(parsed)


def _parse_option(value: Any) -> ModelEditionOption | None:
    if isinstance(value, Mapping):
        option_value = _clean_text(value.get("value") or value.get("model_edition") or value.get("model") or value.get("id"))
        if not option_value:
            return None
        label = _clean_text(value.get("label") or value.get("name")) or option_value
        trim_max_tokens = coerce_positive_int(
            value.get("trim_max_tokens")
            or value.get("context_window_tokens")
            or value.get("max_context_tokens")
        )
        reasoning_efforts = _parse_reasoning_efforts(value.get("reasoning_efforts"))
        agent_capabilities = _parse_agent_capabilities(value.get("agent_capabilities"))
        return ModelEditionOption(
            value=option_value,
            label=label,
            trim_max_tokens=trim_max_tokens,
            reasoning_efforts=reasoning_efforts,
            agent_capabilities=agent_capabilities,
        )
    option_value = _clean_text(value)
    if not option_value:
        return None
    return ModelEditionOption(value=option_value, label=option_value)


def _parse_reasoning_efforts(value: Any) -> ReasoningEffortConfig | None:
    if not isinstance(value, Mapping):
        return None
    options = _parse_reasoning_effort_options(value.get("options"))
    default = _clean_text(value.get("default"))
    disabled_default = _clean_text(value.get("disabled_default"))
    if default is None and not options:
        return None
    return ReasoningEffortConfig(
        default=default or "",
        disabled_default=disabled_default,
        options=options,
    )


def _parse_agent_capabilities(value: Any) -> AgentModelCapabilities | None:
    if not isinstance(value, Mapping):
        return None
    raw_roles = value.get("roles")
    if isinstance(raw_roles, str):
        roles = frozenset(part.strip().lower() for part in raw_roles.split(",") if part.strip())
    elif isinstance(raw_roles, Sequence) and not isinstance(raw_roles, bytes | bytearray):
        roles = frozenset(str(part).strip().lower() for part in raw_roles if str(part).strip())
    else:
        roles = frozenset()
    return AgentModelCapabilities(
        supports_messages=coerce_truthy(value.get("supports_messages", False)),
        roles=roles,
        supports_native_tools=coerce_truthy(value.get("supports_native_tools", False)),
        supports_required_tool_choice=coerce_truthy(value.get("supports_required_tool_choice", False)),
        supports_streamed_tool_calls=coerce_truthy(value.get("supports_streamed_tool_calls", False)),
        supports_non_stream_agent_sample=coerce_truthy(value.get("supports_non_stream_agent_sample", False)),
    )


def _parse_reasoning_effort_options(value: Any) -> tuple[ReasoningEffortOption, ...]:
    if isinstance(value, str):
        candidates: Sequence[Any] = value.replace("\n", ",").split(",")
    elif isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        candidates = value
    else:
        return ()
    parsed: list[ReasoningEffortOption] = []
    for candidate in candidates:
        option = _parse_reasoning_effort_option(candidate)
        if option is not None:
            parsed.append(option)
    return tuple(parsed)


def _parse_reasoning_effort_option(value: Any) -> ReasoningEffortOption | None:
    if isinstance(value, Mapping):
        option_value = _clean_text(value.get("value") or value.get("id"))
        if not option_value:
            return None
        label = _clean_text(value.get("label") or value.get("name")) or option_value
        return ReasoningEffortOption(
            value=option_value,
            label=label,
            allow_when_thinking_disabled=coerce_truthy(value.get("allow_when_thinking_disabled", False)),
        )
    option_value = _clean_text(value)
    if not option_value:
        return None
    return ReasoningEffortOption(value=option_value, label=option_value)


def _container_default(value: Any) -> str | None:
    if not isinstance(value, Mapping):
        return None
    return _clean_text(value.get("default") or value.get("default_model_edition") or value.get("model_edition_default"))


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
