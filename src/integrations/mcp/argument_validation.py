from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from jsonschema import Draft202012Validator, Draft7Validator, SchemaError, ValidationError


class MCPToolArgumentValidationError(ValueError):
    def __init__(self) -> None:
        super().__init__("MCP tool arguments are invalid")


def validate_mcp_tool_arguments(
    schema: Mapping[str, Any],
    arguments: Mapping[str, Any],
) -> None:
    try:
        validator_type = Draft202012Validator if "$schema" in schema else Draft7Validator
        schema_snapshot = _plain_json_value(schema)
        validator_type.check_schema(schema_snapshot)
        validator_type(schema_snapshot).validate(dict(arguments))
    except (SchemaError, ValidationError) as exc:
        raise MCPToolArgumentValidationError() from exc


def _plain_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain_json_value(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_plain_json_value(item) for item in value]
    return value


__all__ = [
    "MCPToolArgumentValidationError",
    "validate_mcp_tool_arguments",
]
