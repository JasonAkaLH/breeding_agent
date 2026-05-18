from __future__ import annotations

from typing import Any, Mapping

RESPONSE_ROLE_METADATA_KEY = "response_role"
RESPONSE_ROLE_INTERMEDIATE = "intermediate"
RESPONSE_ROLE_FINAL = "final"

ANSWER_SCOPE_METADATA_KEY = "answer_scope"
AUTO_SKILL_MATCHING_ENABLED_METADATA_KEY = "auto_skill_matching_enabled"


def response_role_from_metadata(metadata: Mapping[str, Any] | None) -> str | None:
    value = (metadata or {}).get(RESPONSE_ROLE_METADATA_KEY)
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if normalized in {RESPONSE_ROLE_INTERMEDIATE, RESPONSE_ROLE_FINAL}:
        return normalized
    return None


def answer_scope_from_metadata(metadata: Mapping[str, Any] | None) -> str | None:
    value = (metadata or {}).get(ANSWER_SCOPE_METADATA_KEY)
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def auto_skill_matching_enabled(metadata: Mapping[str, Any] | None) -> bool:
    value = (metadata or {}).get(AUTO_SKILL_MATCHING_ENABLED_METADATA_KEY)
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"0", "false", "no", "off"}:
            return False
        if normalized in {"1", "true", "yes", "on"}:
            return True
    return bool(value)
