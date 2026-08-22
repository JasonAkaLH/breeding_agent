from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any


AGENT_PAYLOAD_MAX_BYTES = 131_072


class AgentPayloadError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CanonicalAgentPayload:
    json_text: str
    size_bytes: int
    sha256: str


def canonicalize_agent_payload(value: Any) -> CanonicalAgentPayload:
    _validate_json_value(value)
    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ) + "\n"
    except (TypeError, ValueError) as exc:
        raise AgentPayloadError("agent_payload_not_strict_json") from exc
    encoded = text.encode("utf-8")
    if len(encoded) > AGENT_PAYLOAD_MAX_BYTES:
        raise AgentPayloadError(
            f"agent_payload_too_large:{len(encoded)}>{AGENT_PAYLOAD_MAX_BYTES}"
        )
    return CanonicalAgentPayload(
        json_text=text,
        size_bytes=len(encoded),
        sha256=hashlib.sha256(encoded).hexdigest(),
    )


def _validate_json_value(value: Any) -> None:
    if value is None or isinstance(value, str | bool | int):
        return
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            raise AgentPayloadError("agent_payload_non_finite_number")
        return
    if isinstance(value, list | tuple):
        for item in value:
            _validate_json_value(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise AgentPayloadError("agent_payload_object_key_not_string")
            _validate_json_value(item)
        return
    raise AgentPayloadError(f"agent_payload_unsupported_type:{type(value).__name__}")
