from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


MCP_SERVER_BINDING_METADATA_KEY = "mcp_server_binding"
MCP_SERVER_BINDING_CONTEXT_METADATA_KEY = "mcp_server_binding_context"
MCP_SERVER_BADGE_METADATA_KEY = "mcp_server_badge"
MCP_BINDING_MODE_EXPLICIT_COMMAND = "explicit_command"
MCP_BINDING_REQUEST_ALLOWED_METADATA_KEYS = frozenset(
    {
        MCP_SERVER_BINDING_METADATA_KEY,
        "upload_ids",
        "upload_sheet_selections",
        "deep_thinking",
        "main_agent_reasoning_effort",
    }
)
MCP_SERVER_ID_MAX_UTF8_BYTES = 128
MCP_SERVER_DISPLAY_NAME_MAX_CHARS = 100
MCP_SERVER_COMMAND_MAX_CHARS = 101


class MCPBoundServerUnavailableError(RuntimeError):
    code = "mcp_bound_server_unavailable"


class MCPBindingFeatureUnavailableError(RuntimeError):
    code = "mcp_feature_unavailable"


class MCPPersistedBindingError(RuntimeError):
    pass


@dataclass(slots=True, frozen=True)
class ResolvedMCPServerBinding:
    server_id: str
    server_config_version: int
    server_security_version: int
    display_name: str
    command: str
    binding_mode: str = MCP_BINDING_MODE_EXPLICIT_COMMAND

    def private_context(self) -> dict[str, object]:
        return {
            "server_id": self.server_id,
            "server_config_version": self.server_config_version,
            "server_security_version": self.server_security_version,
            "binding_mode": self.binding_mode,
        }

    def public_badge(self) -> dict[str, object]:
        return {
            "server_id": self.server_id,
            "display_name": self.display_name,
            "command": self.command,
            "binding_mode": self.binding_mode,
        }


def normalize_mcp_server_id(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("server_id must be a string")
    normalized = value.strip()
    if (
        not normalized
        or len(normalized.encode("utf-8")) > MCP_SERVER_ID_MAX_UTF8_BYTES
        or _contains_control_characters(normalized)
    ):
        raise ValueError(
            f"server_id must be non-empty, contain no control characters, and be at most {MCP_SERVER_ID_MAX_UTF8_BYTES} UTF-8 bytes"
        )
    return normalized


def build_resolved_mcp_server_binding(server: Any) -> ResolvedMCPServerBinding:
    server_id = normalize_mcp_server_id(getattr(server, "server_id", None))
    display_name = _normalize_display_name(getattr(server, "display_name", None))
    config_version = _positive_int(getattr(server, "config_version", None), field_name="server_config_version")
    security_version = _positive_int(
        getattr(server, "security_version", None),
        field_name="server_security_version",
    )
    return ResolvedMCPServerBinding(
        server_id=server_id,
        server_config_version=config_version,
        server_security_version=security_version,
        display_name=display_name,
        command=f"${display_name}",
    )


def parse_persisted_mcp_server_binding_context(value: object) -> ResolvedMCPServerBinding:
    if not isinstance(value, Mapping):
        raise MCPPersistedBindingError("mcp_server_binding_context_missing")
    if set(value) != {
        "server_id",
        "server_config_version",
        "server_security_version",
        "binding_mode",
    }:
        raise MCPPersistedBindingError("mcp_server_binding_context_invalid")
    try:
        server_id = normalize_mcp_server_id(value.get("server_id"))
        config_version = _positive_int(
            value.get("server_config_version"),
            field_name="server_config_version",
        )
        security_version = _positive_int(
            value.get("server_security_version"),
            field_name="server_security_version",
        )
    except ValueError as exc:
        raise MCPPersistedBindingError("mcp_server_binding_context_invalid") from exc
    if value.get("binding_mode") != MCP_BINDING_MODE_EXPLICIT_COMMAND:
        raise MCPPersistedBindingError("mcp_server_binding_context_invalid")
    return ResolvedMCPServerBinding(
        server_id=server_id,
        server_config_version=config_version,
        server_security_version=security_version,
        display_name="",
        command="",
    )


def safe_public_mcp_server_badge(value: object) -> dict[str, object] | None:
    if not isinstance(value, Mapping) or set(value) != {
        "server_id",
        "display_name",
        "command",
        "binding_mode",
    }:
        return None
    try:
        server_id = normalize_mcp_server_id(value.get("server_id"))
        display_name = _normalize_display_name(value.get("display_name"))
    except ValueError:
        return None
    command = value.get("command")
    if (
        not isinstance(command, str)
        or command != f"${display_name}"
        or len(command) > MCP_SERVER_COMMAND_MAX_CHARS
        or _contains_control_characters(command)
        or value.get("binding_mode") != MCP_BINDING_MODE_EXPLICIT_COMMAND
    ):
        return None
    return {
        "server_id": server_id,
        "display_name": display_name,
        "command": command,
        "binding_mode": MCP_BINDING_MODE_EXPLICIT_COMMAND,
    }


def _normalize_display_name(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("display_name must be a string")
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > MCP_SERVER_DISPLAY_NAME_MAX_CHARS
        or _contains_control_characters(normalized)
    ):
        raise ValueError("display_name is invalid")
    return normalized


def _positive_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _contains_control_characters(value: str) -> bool:
    return any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value)


__all__ = [
    "MCP_BINDING_MODE_EXPLICIT_COMMAND",
    "MCP_BINDING_REQUEST_ALLOWED_METADATA_KEYS",
    "MCPBindingFeatureUnavailableError",
    "MCPBoundServerUnavailableError",
    "MCPPersistedBindingError",
    "MCP_SERVER_BADGE_METADATA_KEY",
    "MCP_SERVER_BINDING_CONTEXT_METADATA_KEY",
    "MCP_SERVER_BINDING_METADATA_KEY",
    "ResolvedMCPServerBinding",
    "build_resolved_mcp_server_binding",
    "normalize_mcp_server_id",
    "parse_persisted_mcp_server_binding_context",
    "safe_public_mcp_server_badge",
]
