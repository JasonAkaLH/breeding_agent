from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ModelEditionOption:
    value: str
    label: str


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

    Legacy top-level ``model_edition`` / ``model`` remains a single-option fallback
    for old tests and smoke scripts, but it does not define product choices.
    """

    config = config or {}
    for key in _MODEL_EDITION_CONTAINER_KEYS:
        parsed = _parse_options_container(config.get(key))
        if parsed:
            return parsed

    legacy = _clean_text(config.get("model_edition") or config.get("model"))
    if legacy:
        return (ModelEditionOption(value=legacy, label=legacy),)
    return ()


def configured_model_editions(config: Mapping[str, Any] | None = None) -> tuple[str, ...]:
    return tuple(option.value for option in model_edition_options(config))


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
    return next_config


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
        return ModelEditionOption(value=option_value, label=label)
    option_value = _clean_text(value)
    if not option_value:
        return None
    return ModelEditionOption(value=option_value, label=option_value)


def _container_default(value: Any) -> str | None:
    if not isinstance(value, Mapping):
        return None
    return _clean_text(value.get("default") or value.get("default_model_edition") or value.get("model_edition_default"))


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
