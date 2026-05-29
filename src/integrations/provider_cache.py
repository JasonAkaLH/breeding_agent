from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from src.core.coercion import coerce_truthy as truthy


def resolve_provider_cache_capabilities(config: Mapping[str, Any]) -> dict[str, Any]:
    raw: Any = None
    for key in (
        "provider_cache_capabilities",
        "llm_cache_capabilities",
        "prompt_cache",
        "cache",
    ):
        value = config.get(key)
        if isinstance(value, Mapping):
            raw = value
            break
    raw_mapping = raw if isinstance(raw, Mapping) else {}
    supports_prompt_cache = _capability_flag(
        raw_mapping,
        config,
        ("supports_prompt_cache", "prompt_cache_supported", "supports_cache_hint", "cache_hint_supported"),
        default=False,
    )
    prompt_cache_hint_enabled = _capability_flag(
        raw_mapping,
        config,
        ("prompt_cache_hint_enabled", "cache_hint_enabled", "enable_prompt_cache_hint", "enabled"),
        default=False,
    )
    hint = (
        raw_mapping.get("prompt_cache_hint")
        if "prompt_cache_hint" in raw_mapping
        else raw_mapping.get("cache_hint")
        if "cache_hint" in raw_mapping
        else raw_mapping.get("request_extra_body")
        if "request_extra_body" in raw_mapping
        else raw_mapping.get("hint")
    )
    return {
        "supports_prompt_cache": supports_prompt_cache,
        "prompt_cache_hint_enabled": prompt_cache_hint_enabled,
        "prompt_cache_hint": coerce_cache_hint(hint),
    }


def provider_cache_capabilities_metadata(
    capabilities_or_config: Mapping[str, Any] | None,
    *,
    status: object | None = None,
) -> dict[str, Any]:
    if not isinstance(capabilities_or_config, Mapping):
        capabilities: dict[str, Any] = resolve_provider_cache_capabilities({})
    elif any(key in capabilities_or_config for key in ("prompt_cache_hint", "supports_prompt_cache", "prompt_cache_hint_enabled")):
        capabilities = dict(capabilities_or_config)
    else:
        capabilities = resolve_provider_cache_capabilities(capabilities_or_config)
    enabled = truthy(capabilities.get("prompt_cache_hint_enabled"))
    supports = truthy(capabilities.get("supports_prompt_cache"))
    resolved_status = str(status or provider_cache_hint_status(capabilities)).strip() or "disabled"
    metadata: dict[str, Any] = {
        "supports_prompt_cache": supports,
        "prompt_cache_hint_enabled": enabled,
        "status": resolved_status,
    }
    hint_keys = cache_hint_keys(capabilities.get("prompt_cache_hint")) or coerce_hint_keys(capabilities.get("hint_keys"))
    if hint_keys:
        metadata["hint_keys"] = hint_keys
    return metadata


def provider_cache_hint_status(cache_capabilities: Mapping[str, Any]) -> str:
    if not truthy(cache_capabilities.get("prompt_cache_hint_enabled")):
        return "disabled"
    if not truthy(cache_capabilities.get("supports_prompt_cache")):
        return "unsupported"
    return "enabled"


def coerce_cache_hint(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): coerce_cache_hint(item)
            for key, item in value.items()
            if isinstance(key, str | int | float | bool)
        }
    if isinstance(value, list | tuple):
        return [
            coerce_cache_hint(item)
            for item in value
            if isinstance(item, str | int | float | bool | Mapping | list | tuple) or item is None
        ]
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return None


def cache_hint_keys(value: Any) -> list[str]:
    if isinstance(value, Mapping):
        if isinstance(value.get("extra_body"), Mapping):
            return sorted(str(key) for key in value["extra_body"].keys())
        return sorted(str(key) for key in value.keys())
    if value not in (None, ""):
        return ["value"]
    return []


def coerce_hint_keys(value: Any) -> list[str]:
    if isinstance(value, str):
        candidates: Sequence[Any] = value.replace("\n", ",").split(",")
    elif isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        candidates = value
    else:
        return []
    return sorted({str(item).strip() for item in candidates if str(item).strip()})


def _capability_flag(
    raw_mapping: Mapping[str, Any],
    config: Mapping[str, Any],
    keys: tuple[str, ...],
    *,
    default: bool,
) -> bool:
    for key in keys:
        if key in raw_mapping:
            return truthy(raw_mapping[key])
        if key in config:
            return truthy(config[key])
    return default
