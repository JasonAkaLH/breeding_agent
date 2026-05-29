from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from src.core.coercion import coerce_truthy


def safe_role_capabilities(value: Mapping[str, Any] | tuple[str, ...] | None) -> dict[str, Any]:
    if isinstance(value, Mapping):
        safe: dict[str, Any] = {}
        if "supports_messages" in value:
            safe["supports_messages"] = coerce_truthy(value.get("supports_messages"))
        elif "messages_supported" in value:
            safe["supports_messages"] = coerce_truthy(value.get("messages_supported"))
        role_list = safe_role_list(
            value.get("roles")
            or value.get("supported_roles")
            or value.get("message_roles")
            or value.get("supported_message_roles")
        )
        if role_list:
            safe["roles"] = role_list
        return safe
    role_list = safe_role_list(value)
    return {"roles": role_list} if role_list else {}


def safe_role_list(value: Any) -> list[str]:
    if isinstance(value, str):
        candidates = value.replace("\n", ",").split(",")
    elif isinstance(value, list | tuple | set | frozenset):
        candidates = value
    else:
        return []
    return sorted({str(role).strip().lower() for role in candidates if str(role).strip()})
