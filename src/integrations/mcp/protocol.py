from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

MCP_PROTOCOL_VERSION = "2025-11-25"
SUPPORTED_MCP_PROTOCOL_VERSIONS = frozenset({MCP_PROTOCOL_VERSION})
JSONRPC_VERSION = "2.0"


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


def json_rpc_error(*, request_id: str | int | None, code: int, message: str, data: Mapping[str, Any] | None = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data:
        error["data"] = dict(data)
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "error": error}


def json_rpc_result(*, request_id: str | int | None, result: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": dict(result or {})}


def json_rpc_message_kind(message: Any) -> str:
    if isinstance(message, list):
        raise ValueError("JSON-RPC batch arrays are unsupported by MCP Streamable HTTP.")
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
