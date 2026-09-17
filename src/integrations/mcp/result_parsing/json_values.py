from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import Any

from .errors import MCPResultParseError


MAX_JSON_DEPTH = 64
MAX_JSON_NODES = 100_000
MAX_JSON_KEY_CODE_POINTS = 1_024


def strict_json_value(value: object) -> Any:
    nodes = 0

    def visit(item: object, depth: int) -> Any:
        nonlocal nodes
        if depth > MAX_JSON_DEPTH:
            raise MCPResultParseError("malformed_json")
        nodes += 1
        if nodes > MAX_JSON_NODES:
            raise MCPResultParseError("malformed_json")
        if item is None or isinstance(item, bool) or isinstance(item, int):
            return item
        if isinstance(item, float):
            if not math.isfinite(item):
                raise MCPResultParseError("malformed_json")
            return item
        if isinstance(item, str):
            _require_unicode_scalar_string(item)
            return item
        if isinstance(item, Mapping):
            normalized: dict[str, Any] = {}
            for key, nested in item.items():
                if not isinstance(key, str):
                    raise MCPResultParseError("malformed_json")
                _require_unicode_scalar_string(key)
                if len(key) > MAX_JSON_KEY_CODE_POINTS or key in normalized:
                    raise MCPResultParseError("malformed_json")
                normalized[key] = visit(nested, depth + 1)
            return normalized
        if isinstance(item, (list, tuple)):
            return [visit(nested, depth + 1) for nested in item]
        raise MCPResultParseError("malformed_json")

    return visit(value, 0)


def loads_strict_json(payload: bytes | str) -> Any:
    try:
        text = payload.decode("utf-8", "strict") if isinstance(payload, bytes) else payload

        def pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise MCPResultParseError("malformed_json")
                result[key] = value
            return result

        value = json.loads(
            text,
            object_pairs_hook=pairs_hook,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                MCPResultParseError("malformed_json")
            ),
        )
    except MCPResultParseError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise MCPResultParseError("malformed_json") from exc
    return strict_json_value(value)


def canonical_json_bytes(value: object) -> bytes:
    normalized = strict_json_value(value)
    try:
        return json.dumps(
            normalized,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise MCPResultParseError("malformed_json") from exc


def _require_unicode_scalar_string(value: str) -> None:
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise MCPResultParseError("malformed_json")
