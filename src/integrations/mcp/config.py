from __future__ import annotations

import os
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .protocol import (
    DEFAULT_MCP_PROTOCOL_VERSION,
    MCP_PROTOCOL_VERSION,
    MCP_TRANSPORT_LEGACY_HTTP_SSE,
    MCP_TRANSPORT_STDIO,
    MCP_TRANSPORT_STREAMABLE_HTTP,
    is_mcp_transport_family_allowed,
    validate_mcp_protocol_version,
)
from .sidecar import MCPRustRuntimeSettings

_SERVER_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_CAPABILITY_ID_RE = re.compile(r"^mcp\.[a-z0-9_.-]+$")
_HTTP_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_ALLOWED_AUTH_TYPES = frozenset({"none", "bearer_env", "api_key_env", "preconfigured"})
_ALLOWED_TRANSPORTS = frozenset({MCP_TRANSPORT_STREAMABLE_HTTP, MCP_TRANSPORT_STDIO, MCP_TRANSPORT_LEGACY_HTTP_SSE})
_DANGEROUS_REQUEST_HEADERS = frozenset(
    {
        "host",
        "content-length",
        "transfer-encoding",
        "connection",
        "upgrade",
        "proxy-authorization",
        "authorization",
        "accept",
        "content-type",
        "mcp-protocol-version",
        "mcp-session-id",
        "last-event-id",
    }
)
_DANGEROUS_AUTH_HEADERS = _DANGEROUS_REQUEST_HEADERS - frozenset({"authorization"})
MCP_SERVER_CONFIG_ENV = "MAF_MCP_SERVER_CONFIG_PATH"
MCP_SERVER_CONFIG_DEFAULT_PATH = "mcp_server_config.json"


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
            token_env=str(_first_alias(raw, "token_env", "tokenEnv") or "").strip(),
            api_key_env=str(_first_alias(raw, "api_key_env", "apiKeyEnv") or "").strip(),
            header_name=str(_first_alias(raw, "header_name", "headerName", "api_key_header", "apiKeyHeader") or "").strip(),
        )

    def validation_error(self) -> str:
        if self.type not in _ALLOWED_AUTH_TYPES:
            return f"Unsupported MCP auth type: {self.type}"
        if self.type == "bearer_env" and not self.token_env:
            return "bearer_env auth requires token_env."
        if self.type == "api_key_env" and (not self.api_key_env or not self.header_name):
            return "api_key_env auth requires api_key_env and header_name."
        if self.type == "api_key_env":
            if not _HTTP_HEADER_NAME_RE.fullmatch(self.header_name):
                return f"Invalid MCP auth header name: {self.header_name}"
            if self.header_name.lower() in _DANGEROUS_AUTH_HEADERS:
                return f"Unsupported MCP auth header: {self.header_name}"
        return ""

    def headers(self) -> dict[str, str]:
        if self.type == "bearer_env":
            token = os.environ.get(self.token_env, "")
            return {"Authorization": f"Bearer {token}"} if token else {}
        if self.type == "api_key_env":
            token = os.environ.get(self.api_key_env, "")
            return {self.header_name: token} if token else {}
        return {}

    def header_names(self) -> tuple[str, ...]:
        if self.type == "bearer_env":
            return ("Authorization",)
        if self.type == "api_key_env" and self.header_name:
            return (self.header_name,)
        return ()


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
    protocol_version_pinned: bool = True
    allow_http_localhost: bool = False
    request_headers: Mapping[str, str] = field(default_factory=dict)
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
        raw_protocol_version = str(_first_alias(raw, "protocol_version", "protocolVersion") or "").strip()
        protocol_version = raw_protocol_version or DEFAULT_MCP_PROTOCOL_VERSION
        endpoint = str(_first_alias(raw, "endpoint", "url") or endpoint).strip()
        request_headers = _coerce_request_headers(
            _first_alias(raw, "request_headers", "requestHeaders", "headers") if any(key in raw for key in ("request_headers", "requestHeaders", "headers")) else None
        )
        return cls(
            server_id=str(_first_alias(raw, "server_id", "serverId") or "").strip(),
            enabled=bool(raw.get("enabled", True)),
            required=bool(raw.get("required", False)),
            transport=str(raw.get("transport") or "streamable_http").strip().lower(),
            endpoint=endpoint,
            endpoint_env=endpoint_env,
            protocol_version=protocol_version,
            protocol_version_pinned=bool(raw_protocol_version),
            allow_http_localhost=bool(raw.get("allow_http_localhost", False)),
            request_headers=request_headers,
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
        try:
            validate_mcp_protocol_version(self.protocol_version)
        except ValueError as exc:
            return str(exc)
        if not is_mcp_transport_family_allowed(self.protocol_version, self.transport):
            return f"MCP transport {self.transport} is incompatible with protocol_version {self.protocol_version}."
        if self.transport == MCP_TRANSPORT_STDIO:
            return "stdio MCP transport is reserved for a sandboxed Phase 2 implementation."
        if not self.endpoint:
            return "MCP server endpoint is required."
        endpoint_error = _validate_endpoint(self.endpoint)
        if endpoint_error:
            return endpoint_error
        auth_error = self.auth.validation_error()
        if auth_error:
            return auth_error
        header_error = _validate_request_headers(self.request_headers)
        if header_error:
            return header_error
        conflict = _header_conflict(self.request_headers, self.auth.header_names())
        if conflict:
            return f"MCP request header {conflict} conflicts with auth header."
        enabled_capabilities = [name for name, enabled in self.client_capabilities.items() if enabled]
        unsupported = sorted(set(enabled_capabilities))
        if unsupported:
            return f"Unsupported MCP client capabilities enabled: {', '.join(unsupported)}"
        return ""

    def refreshes_on_conversation_start(self) -> bool:
        return self.enabled and self.discovery.refresh_on_conversation_start

    @property
    def transport_security(self) -> str:
        scheme = urlparse(self.endpoint).scheme.lower()
        if scheme == "http":
            return "plaintext_http"
        if scheme == "https":
            return "tls_http"
        return scheme or "unknown"

    @property
    def request_header_names(self) -> tuple[str, ...]:
        return tuple(self.request_headers)

    @property
    def credential_over_plaintext_http(self) -> bool:
        return self.transport_security == "plaintext_http" and bool(self.auth.header_names())


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
            rust_runtime=MCPRustRuntimeSettings.from_mapping(
                raw.get("rust_runtime") if isinstance(raw.get("rust_runtime"), Mapping) else None
            ),
        )

    def refreshes_on_conversation_start(self) -> bool:
        return any(server.refreshes_on_conversation_start() for server in self.servers)


def load_mcp_server_config(
    *,
    path: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    cwd: str | os.PathLike[str] | None = None,
    base_config: MCPRuntimeConfig | Mapping[str, Any] | None = None,
) -> MCPRuntimeConfig:
    """Load external mcp_server_config.json and normalize it to MCPRuntimeConfig."""

    base = base_config if isinstance(base_config, MCPRuntimeConfig) else MCPRuntimeConfig.from_mapping(base_config)
    resolved = _resolve_mcp_server_config_path(path=path, env=env, cwd=cwd)
    if resolved is None:
        return base
    raw = json.loads(resolved.read_text(encoding="utf-8"))
    external = _runtime_config_from_external_server_config(raw)
    return _merge_runtime_configs(base, external)


def _validate_endpoint(endpoint: str) -> str:
    parsed = urlparse(endpoint)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return ""
    return "MCP server endpoint must be an absolute http(s) URL."


def _resolve_mcp_server_config_path(
    *,
    path: str | os.PathLike[str] | None,
    env: Mapping[str, str] | None,
    cwd: str | os.PathLike[str] | None,
) -> Path | None:
    if path is not None:
        resolved = Path(path)
        if not resolved.exists():
            raise FileNotFoundError(str(resolved))
        return resolved
    environ = env if env is not None else os.environ
    configured = str(environ.get(MCP_SERVER_CONFIG_ENV) or "").strip()
    if configured:
        resolved = Path(configured)
        if not resolved.exists():
            raise FileNotFoundError(str(resolved))
        return resolved
    default = Path(cwd or ".") / MCP_SERVER_CONFIG_DEFAULT_PATH
    return default if default.exists() else None


def _runtime_config_from_external_server_config(payload: Any) -> MCPRuntimeConfig:
    if not isinstance(payload, Mapping):
        raise ValueError("mcp_server_config.json must contain a JSON object.")
    servers_payload = payload.get("mcpServers")
    if not isinstance(servers_payload, Mapping):
        raise ValueError("mcp_server_config.json requires object field mcpServers.")
    servers = []
    for server_id, raw_config in servers_payload.items():
        if not isinstance(raw_config, Mapping):
            raise ValueError(f"MCP server {server_id} config must be an object.")
        normalized = _normalize_external_server_config(str(server_id), raw_config)
        servers.append(normalized)
    return MCPRuntimeConfig.from_mapping(
        {
            "enabled": bool(payload.get("enabled", True)),
            "default_timeout_seconds": payload.get("defaultTimeoutSeconds", payload.get("default_timeout_seconds", 20)),
            "servers": servers,
        }
    )


def _normalize_external_server_config(server_id: str, raw_config: Mapping[str, Any]) -> dict[str, Any]:
    raw = dict(raw_config)
    explicit_server_id = _first_alias(raw, "server_id", "serverId")
    if explicit_server_id is not None and str(explicit_server_id).strip() != server_id:
        raise ValueError(f"MCP server_id conflict for {server_id}.")
    protocol_version = _conflict_checked_alias(raw, "protocol_version", "protocolVersion")
    auth = _normalize_auth_aliases(raw.get("auth") if isinstance(raw.get("auth"), Mapping) else None)
    normalized: dict[str, Any] = {
        "server_id": server_id,
        "endpoint": _required_alias(raw, "url", "endpoint"),
        "enabled": raw.get("enabled", True),
        "transport": raw.get("transport", "streamable_http"),
        "auth": auth,
    }
    if protocol_version is not None:
        normalized["protocol_version"] = protocol_version
    if "headers" in raw:
        normalized["headers"] = raw.get("headers")
    for key in (
        "required",
        "allow_http_localhost",
        "client_capabilities",
        "trust_level",
        "discovery",
        "limits",
        "tools",
    ):
        if key in raw:
            normalized[key] = raw[key]
    return normalized


def _merge_runtime_configs(base: MCPRuntimeConfig, external: MCPRuntimeConfig) -> MCPRuntimeConfig:
    if not external.servers:
        return base
    seen: set[str] = set()
    for server in base.servers:
        seen.add(server.server_id)
    duplicates = sorted(server.server_id for server in external.servers if server.server_id in seen)
    if duplicates:
        raise ValueError(f"Duplicate MCP server_id across config sources: {', '.join(duplicates)}")
    return MCPRuntimeConfig(
        enabled=base.enabled or external.enabled,
        default_timeout_seconds=external.default_timeout_seconds if not base.enabled and not base.servers else base.default_timeout_seconds,
        servers=(*base.servers, *external.servers),
        rust_runtime=base.rust_runtime,
    )


def _normalize_auth_aliases(payload: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        return {}
    raw = dict(payload)
    normalized = {"type": raw.get("type", "none")}
    for canonical, alias in (("token_env", "tokenEnv"), ("api_key_env", "apiKeyEnv"), ("header_name", "headerName")):
        value = _conflict_checked_alias(raw, canonical, alias)
        if value is not None:
            normalized[canonical] = value
    return normalized


def _required_alias(raw: Mapping[str, Any], *names: str) -> Any:
    value = _conflict_checked_alias(raw, *names)
    if value is None or str(value).strip() == "":
        raise ValueError(f"Missing required MCP server config field: {names[0]}.")
    return value


def _conflict_checked_alias(raw: Mapping[str, Any], *names: str) -> Any:
    present = [(name, raw[name]) for name in names if name in raw]
    if not present:
        return None
    first_name, first_value = present[0]
    for name, value in present[1:]:
        if value != first_value:
            raise ValueError(f"conflicting {name} values in MCP server config.")
    return first_value


def _first_alias(raw: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in raw:
            return raw[name]
    return None


def _coerce_request_headers(value: Any) -> Mapping[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): str(header_value) for key, header_value in value.items()}


def _validate_request_headers(headers: Mapping[str, str]) -> str:
    for name, value in headers.items():
        normalized = str(name).strip()
        if not _HTTP_HEADER_NAME_RE.fullmatch(normalized):
            return f"Invalid MCP request header name: {normalized}"
        if normalized.lower() in _DANGEROUS_REQUEST_HEADERS:
            return f"Unsupported MCP request header: {normalized}"
        if "\r" in str(value) or "\n" in str(value):
            return f"Invalid MCP request header value for: {normalized}"
    return ""


def _header_conflict(headers: Mapping[str, str], auth_header_names: tuple[str, ...]) -> str:
    configured = {name.lower(): name for name in headers}
    for auth_name in auth_header_names:
        if auth_name.lower() in configured:
            return configured[auth_name.lower()]
    return ""


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
