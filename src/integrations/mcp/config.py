from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from .protocol import MCP_PROTOCOL_VERSION, SUPPORTED_MCP_PROTOCOL_VERSIONS
from .sidecar import MCPRustRuntimeSettings

_SERVER_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_CAPABILITY_ID_RE = re.compile(r"^mcp\.[a-z0-9_.-]+$")
_ALLOWED_AUTH_TYPES = frozenset({"none", "bearer_env", "api_key_env", "preconfigured"})
_ALLOWED_TRANSPORTS = frozenset({"streamable_http", "stdio"})


@dataclass(slots=True, frozen=True)
class MCPAuthConfig:
    type: str = "none"
    token_env: str = ""
    api_key_env: str = ""
    header_name: str = ""

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any] | None) -> "MCPAuthConfig":
        raw = dict(payload or {})
        auth_type = str(raw.get("type") or "none").strip().lower()
        if auth_type == "bearer":
            auth_type = "bearer_env"
        if auth_type == "api_key":
            auth_type = "api_key_env"
        if auth_type == "oauth":
            # Phase 1 deliberately fails closed for interactive OAuth.
            auth_type = "unsupported_oauth"
        return cls(
            type=auth_type,
            token_env=str(raw.get("token_env") or "").strip(),
            api_key_env=str(raw.get("api_key_env") or "").strip(),
            header_name=str(raw.get("header_name") or raw.get("api_key_header") or "").strip(),
        )

    def validation_error(self) -> str:
        if self.type not in _ALLOWED_AUTH_TYPES:
            return f"Unsupported MCP auth type: {self.type}"
        if self.type == "bearer_env" and not self.token_env:
            return "bearer_env auth requires token_env."
        if self.type == "api_key_env" and (not self.api_key_env or not self.header_name):
            return "api_key_env auth requires api_key_env and header_name."
        return ""

    def headers(self) -> dict[str, str]:
        if self.type == "bearer_env":
            token = os.environ.get(self.token_env, "")
            return {"Authorization": f"Bearer {token}"} if token else {}
        if self.type == "api_key_env":
            token = os.environ.get(self.api_key_env, "")
            return {self.header_name: token} if token else {}
        return {}


@dataclass(slots=True, frozen=True)
class MCPDiscoveryConfig:
    refresh_on_startup: bool = True
    refresh_on_conversation_start: bool = False

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any] | None) -> "MCPDiscoveryConfig":
        raw = dict(payload or {})
        return cls(
            refresh_on_startup=bool(raw.get("refresh_on_startup", True)),
            refresh_on_conversation_start=bool(raw.get("refresh_on_conversation_start", False)),
        )


@dataclass(slots=True, frozen=True)
class MCPLimitsConfig:
    max_calls_per_task: int = 5
    max_output_bytes: int = 65_536
    timeout_seconds: float | None = None

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any] | None) -> "MCPLimitsConfig":
        raw = dict(payload or {})
        return cls(
            max_calls_per_task=_positive_int(raw.get("max_calls_per_task"), 5),
            max_output_bytes=_positive_int(raw.get("max_output_bytes"), 65_536),
            timeout_seconds=_positive_float_or_none(raw.get("timeout_seconds")),
        )


@dataclass(slots=True, frozen=True)
class MCPToolConfig:
    tool_name: str
    expose: bool = False
    mode: str = "generic"
    capability_id: str = ""
    public_name: str = ""
    public_description: str = ""
    risk_level: str = "read_only"
    planner_allowed_fields: tuple[str, ...] = ()
    input_schema: Mapping[str, Any] | None = None
    output_schema: Mapping[str, Any] | None = None
    max_output_bytes: int | None = None
    task_augmented_mode: str = "disabled"
    task_ttl_ms: int = 60000
    task_max_polls: int = 20

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "MCPToolConfig":
        raw = dict(payload or {})
        return cls(
            tool_name=str(raw.get("tool_name") or raw.get("name") or "").strip(),
            expose=bool(raw.get("expose", False)),
            mode=str(raw.get("mode") or "generic").strip(),
            capability_id=str(raw.get("capability_id") or "").strip(),
            public_name=str(raw.get("public_name") or raw.get("name") or "").strip(),
            public_description=str(raw.get("public_description") or "").strip(),
            risk_level=str(raw.get("risk_level") or "read_only").strip().lower(),
            planner_allowed_fields=tuple(str(field).strip() for field in raw.get("planner_allowed_fields") or () if str(field).strip()),
            input_schema=raw.get("input_schema") if isinstance(raw.get("input_schema"), Mapping) else None,
            output_schema=raw.get("output_schema") if isinstance(raw.get("output_schema"), Mapping) else None,
            max_output_bytes=_positive_int_or_none(raw.get("max_output_bytes")),
            task_augmented_mode=str(raw.get("task_augmented_mode") or "disabled").strip().lower(),
            task_ttl_ms=_positive_int(raw.get("task_ttl_ms"), 60000),
            task_max_polls=_positive_int(raw.get("task_max_polls"), 20),
        )

    def effective_capability_id(self, server_id: str) -> str:
        if self.capability_id:
            return self.capability_id
        return f"mcp.{server_id.lower()}.{_slugify(self.tool_name)}"

    @staticmethod
    def valid_capability_id(capability_id: str) -> bool:
        return bool(_CAPABILITY_ID_RE.fullmatch(capability_id))


@dataclass(slots=True, frozen=True)
class MCPServerConfig:
    server_id: str
    enabled: bool = True
    required: bool = False
    transport: str = "streamable_http"
    endpoint: str = ""
    endpoint_env: str = ""
    protocol_version: str = MCP_PROTOCOL_VERSION
    allow_http_localhost: bool = False
    client_capabilities: Mapping[str, bool] = field(default_factory=dict)
    auth: MCPAuthConfig = field(default_factory=MCPAuthConfig)
    trust_level: str = "trusted_internal"
    discovery: MCPDiscoveryConfig = field(default_factory=MCPDiscoveryConfig)
    limits: MCPLimitsConfig = field(default_factory=MCPLimitsConfig)
    tools: tuple[MCPToolConfig, ...] = ()

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "MCPServerConfig":
        raw = dict(payload or {})
        endpoint_env = str(raw.get("endpoint_env") or "").strip()
        endpoint = str(raw.get("endpoint") or "").strip()
        if endpoint_env:
            endpoint = str(os.environ.get(endpoint_env, endpoint)).strip()
        return cls(
            server_id=str(raw.get("server_id") or "").strip(),
            enabled=bool(raw.get("enabled", True)),
            required=bool(raw.get("required", False)),
            transport=str(raw.get("transport") or "streamable_http").strip().lower(),
            endpoint=endpoint,
            endpoint_env=endpoint_env,
            protocol_version=str(raw.get("protocol_version") or MCP_PROTOCOL_VERSION).strip(),
            allow_http_localhost=bool(raw.get("allow_http_localhost", False)),
            client_capabilities={str(key): bool(value) for key, value in dict(raw.get("client_capabilities") or {}).items()},
            auth=MCPAuthConfig.from_mapping(raw.get("auth") if isinstance(raw.get("auth"), Mapping) else None),
            trust_level=str(raw.get("trust_level") or "trusted_internal").strip(),
            discovery=MCPDiscoveryConfig.from_mapping(raw.get("discovery") if isinstance(raw.get("discovery"), Mapping) else None),
            limits=MCPLimitsConfig.from_mapping(raw.get("limits") if isinstance(raw.get("limits"), Mapping) else None),
            tools=tuple(
                MCPToolConfig.from_mapping(item)
                for item in (raw.get("tools") or ())
                if isinstance(item, Mapping)
            ),
        )

    def validation_error(self) -> str:
        if not self.server_id or not _SERVER_ID_RE.fullmatch(self.server_id):
            return f"Invalid MCP server_id: {self.server_id}"
        if self.transport not in _ALLOWED_TRANSPORTS:
            return f"Unsupported MCP transport: {self.transport}"
        if self.transport == "stdio":
            return "stdio MCP transport is reserved for a sandboxed Phase 2 implementation."
        if self.protocol_version not in SUPPORTED_MCP_PROTOCOL_VERSIONS:
            return f"Unsupported MCP protocol_version: {self.protocol_version}"
        if not self.endpoint:
            return "MCP server endpoint is required."
        endpoint_error = _validate_endpoint(self.endpoint, allow_http_localhost=self.allow_http_localhost)
        if endpoint_error:
            return endpoint_error
        auth_error = self.auth.validation_error()
        if auth_error:
            return auth_error
        enabled_capabilities = [name for name, enabled in self.client_capabilities.items() if enabled]
        unsupported = sorted(set(enabled_capabilities))
        if unsupported:
            return f"Unsupported MCP client capabilities enabled: {', '.join(unsupported)}"
        return ""

    def refreshes_on_conversation_start(self) -> bool:
        return self.enabled and self.discovery.refresh_on_conversation_start


@dataclass(slots=True, frozen=True)
class MCPRuntimeConfig:
    enabled: bool = False
    default_timeout_seconds: float = 20
    servers: tuple[MCPServerConfig, ...] = ()
    rust_runtime: MCPRustRuntimeSettings = field(default_factory=MCPRustRuntimeSettings)

    @classmethod
    def disabled(cls) -> "MCPRuntimeConfig":
        return cls(enabled=False)

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any] | None) -> "MCPRuntimeConfig":
        if not isinstance(payload, Mapping) or not payload:
            return cls.disabled()
        raw = dict(payload)
        return cls(
            enabled=bool(raw.get("enabled", False)),
            default_timeout_seconds=_positive_float(raw.get("default_timeout_seconds"), 20),
            servers=tuple(
                MCPServerConfig.from_mapping(item)
                for item in (raw.get("servers") or ())
                if isinstance(item, Mapping)
            ),
            rust_runtime=MCPRustRuntimeSettings.from_mapping(raw.get("rust_runtime") if isinstance(raw.get("rust_runtime"), Mapping) else None),
        )

    def refreshes_on_conversation_start(self) -> bool:
        return any(server.refreshes_on_conversation_start() for server in self.servers)


def _validate_endpoint(endpoint: str, *, allow_http_localhost: bool) -> str:
    parsed = urlparse(endpoint)
    if parsed.scheme == "https":
        return ""
    if parsed.scheme == "http" and allow_http_localhost and parsed.hostname in {"localhost", "127.0.0.1", "::1"}:
        return ""
    return "Remote MCP HTTP endpoints must use https unless localhost is explicitly allowed."


def _positive_int(value: Any, default: int) -> int:
    parsed = _positive_int_or_none(value)
    return default if parsed is None else parsed


def _positive_int_or_none(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _positive_float(value: Any, default: float) -> float:
    parsed = _positive_float_or_none(value)
    return default if parsed is None else parsed


def _positive_float_or_none(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9_-]+", "_", value.lower()).strip("_")
    slug = re.sub(r"_+", "_", slug)
    return slug or "tool"
