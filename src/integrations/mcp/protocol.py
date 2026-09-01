from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

MCP_PROTOCOL_VERSION_2024_11_05 = "2024-11-05"
MCP_PROTOCOL_VERSION_2025_03_26 = "2025-03-26"
MCP_PROTOCOL_VERSION_2025_06_18 = "2025-06-18"
MCP_PROTOCOL_VERSION_2025_11_25 = "2025-11-25"
MCP_PROTOCOL_VERSION_2026_07_28 = "2026-07-28"
DEFAULT_MCP_PROTOCOL_VERSION = MCP_PROTOCOL_VERSION_2025_11_25
MCP_PROTOCOL_VERSION = DEFAULT_MCP_PROTOCOL_VERSION
SUPPORTED_MCP_PROTOCOL_VERSION_ORDER = (
    MCP_PROTOCOL_VERSION_2024_11_05,
    MCP_PROTOCOL_VERSION_2025_03_26,
    MCP_PROTOCOL_VERSION_2025_06_18,
    MCP_PROTOCOL_VERSION_2025_11_25,
    MCP_PROTOCOL_VERSION_2026_07_28,
)
SUPPORTED_MCP_PROTOCOL_VERSIONS = frozenset(
    SUPPORTED_MCP_PROTOCOL_VERSION_ORDER
)
JSONRPC_VERSION = "2.0"

MCP_TRANSPORT_LEGACY_HTTP_SSE = "legacy_http_sse"
MCP_TRANSPORT_STREAMABLE_HTTP = "streamable_http"
MCP_TRANSPORT_STDIO = "stdio"


class MCPCompatibilityStatus(StrEnum):
    SUPPORTED = "supported"
    COMPATIBLE_DEGRADED = "compatible-degraded"
    CONFIG_GATED = "config-gated"
    NOT_SUPPORTED = "not-supported"
    FUTURE = "future"
    NOT_APPLICABLE = "not-applicable"


@dataclass(slots=True, frozen=True)
class MCPStreamEvent:
    event: str | None = None
    event_id: str | None = None
    retry_ms: int | None = None
    data: str = ""
    message: Mapping[str, Any] | None = None
    is_priming: bool = False


@dataclass(slots=True, frozen=True)
class MCPTransportResponse:
    message: Mapping[str, Any] | None
    headers: Mapping[str, str] = field(default_factory=dict)
    sse_retry_ms: int | None = None
    last_event_id: str | None = None
    sse_events: tuple[MCPStreamEvent, ...] = ()


@runtime_checkable
class MCPTransport(Protocol):
    async def send(
        self,
        message: Mapping[str, Any],
        *,
        protocol_version: str,
        session_id: str | None = None,
        timeout_seconds: float | None = None,
        last_event_id: str | None = None,
    ) -> MCPTransportResponse: ...

    async def close(self) -> None: ...


@runtime_checkable
class MCPRequestScopedTransport(Protocol):
    """Transport seam for stateless 2026 requests.

    Implementations must issue one POST per request and may return either a JSON
    response in ``message`` or a request-scoped SSE response in ``sse_events``.
    """

    async def send(
        self,
        message: Mapping[str, Any],
        *,
        protocol_version: str,
        request_headers: Mapping[str, str],
        timeout_seconds: float | None = None,
    ) -> MCPTransportResponse: ...

    async def close(self) -> None: ...


@dataclass(slots=True, frozen=True)
class MCPNegotiatedSession:
    server_id: str
    requested_protocol_version: str
    negotiated_protocol_version: str
    transport_family: str
    server_capabilities: Mapping[str, Any]
    server_info: Mapping[str, Any]
    pinned_protocol_version: bool
    session_id: str | None = None
    legacy_post_endpoint: str | None = None
    last_event_id: str | None = None


def validate_mcp_protocol_version(protocol_version: str) -> str:
    parsed = str(protocol_version or "").strip()
    if parsed not in SUPPORTED_MCP_PROTOCOL_VERSIONS:
        raise ValueError(f"Unsupported MCP protocol_version: {parsed}")
    return parsed


def is_mcp_transport_family_allowed(protocol_version: str, transport_family: str) -> bool:
    try:
        version = validate_mcp_protocol_version(protocol_version)
    except ValueError:
        return False
    family = str(transport_family or "").strip().lower()
    if family == MCP_TRANSPORT_STDIO:
        return True
    if family == MCP_TRANSPORT_LEGACY_HTTP_SSE:
        return version == MCP_PROTOCOL_VERSION_2024_11_05
    if family == MCP_TRANSPORT_STREAMABLE_HTTP:
        return version != MCP_PROTOCOL_VERSION_2024_11_05
    return False


def mcp_remote_transport_family_for_protocol_version(protocol_version: str) -> str:
    version = validate_mcp_protocol_version(protocol_version)
    if version == MCP_PROTOCOL_VERSION_2024_11_05:
        return MCP_TRANSPORT_LEGACY_HTTP_SSE
    return MCP_TRANSPORT_STREAMABLE_HTTP


def mcp_feature_status(protocol_version: str, feature: str) -> MCPCompatibilityStatus:
    version = validate_mcp_protocol_version(protocol_version)
    normalized = str(feature or "").strip().lower().replace("-", "_")
    if normalized in {"ordinary_tools", "tools", "tools/list", "tools_list", "tools/call", "tools_call"}:
        return MCPCompatibilityStatus.SUPPORTED
    if normalized in {"batch", "jsonrpc_batch", "json_rpc_batch"}:
        return MCPCompatibilityStatus.NOT_SUPPORTED
    if normalized == "ping":
        return MCPCompatibilityStatus.SUPPORTED
    if normalized in {"notifications_initialized", "initialized_notification"}:
        return (
            MCPCompatibilityStatus.NOT_APPLICABLE
            if version == MCP_PROTOCOL_VERSION_2026_07_28
            else MCPCompatibilityStatus.SUPPORTED
        )
    if normalized in {"server_to_client_request", "server_request", "sampling/createmessage"}:
        if version == MCP_PROTOCOL_VERSION_2026_07_28:
            return MCPCompatibilityStatus.NOT_SUPPORTED
        return MCPCompatibilityStatus.COMPATIBLE_DEGRADED
    if normalized in {"roots", "sampling"}:
        if version == MCP_PROTOCOL_VERSION_2026_07_28:
            return MCPCompatibilityStatus.NOT_SUPPORTED
        return MCPCompatibilityStatus.CONFIG_GATED
    if normalized in {"server/discover", "server_discover", "list_cache_hint", "mrtr", "input_required"}:
        return (
            MCPCompatibilityStatus.SUPPORTED
            if version == MCP_PROTOCOL_VERSION_2026_07_28
            else MCPCompatibilityStatus.NOT_APPLICABLE
        )
    if normalized in {"tasks", "task_augmented_tools_call"} and version == MCP_PROTOCOL_VERSION_2026_07_28:
        return MCPCompatibilityStatus.CONFIG_GATED
    if normalized == "elicitation" and version == MCP_PROTOCOL_VERSION_2026_07_28:
        return MCPCompatibilityStatus.CONFIG_GATED
    if normalized in {"resources", "prompts", "tasks", "task_augmented_tools_call", "elicitation"}:
        return MCPCompatibilityStatus.FUTURE if version != MCP_PROTOCOL_VERSION_2024_11_05 else MCPCompatibilityStatus.NOT_APPLICABLE
    return MCPCompatibilityStatus.NOT_APPLICABLE


is_transport_family_allowed = is_mcp_transport_family_allowed


def json_rpc_error(*, request_id: str | int | None, code: int, message: str, data: Mapping[str, Any] | None = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data:
        error["data"] = dict(data)
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "error": error}


def json_rpc_result(*, request_id: str | int | None, result: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": dict(result or {})}


def json_rpc_message_kind(message: Any) -> str:
    if isinstance(message, list):
        raise ValueError("JSON-RPC batch arrays are unsupported by this MCP runtime.")
    if not isinstance(message, Mapping):
        raise ValueError("JSON-RPC message must be an object.")
    if message.get("jsonrpc") != JSONRPC_VERSION:
        raise ValueError("JSON-RPC message must use version 2.0.")
    has_id = "id" in message
    has_method = "method" in message
    has_result = "result" in message
    has_error = "error" in message
    if has_method and has_id:
        return "request"
    if has_method and not has_id:
        return "notification"
    if has_id and (has_result or has_error):
        return "response"
    raise ValueError("JSON-RPC message must be a request, notification, response, or error.")


def normalize_json_rpc_response_id(
    message: Mapping[str, Any],
    *,
    expected_request_id: str | int,
) -> Mapping[str, Any] | None:
    """Return a matching response with a type-exact request id."""

    try:
        if json_rpc_message_kind(message) != "response":
            return None
    except ValueError:
        return None
    raw_response_id = message.get("id")
    expected_type = type(expected_request_id)
    raw_type = type(raw_response_id)
    if expected_type in {int, str} and raw_type is expected_type and raw_response_id == expected_request_id:
        return message
    if expected_type is int and raw_type is str and raw_response_id == str(expected_request_id):
        normalized = dict(message)
        normalized["id"] = expected_request_id
        return normalized
    return None
