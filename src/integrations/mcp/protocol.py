from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable

MCP_PROTOCOL_VERSION = "2025-11-25"
SUPPORTED_MCP_PROTOCOL_VERSIONS = frozenset({MCP_PROTOCOL_VERSION})
JSONRPC_VERSION = "2.0"


@dataclass(slots=True, frozen=True)
class MCPTransportResponse:
    message: Mapping[str, Any] | None
    headers: Mapping[str, str] = field(default_factory=dict)
    sse_retry_ms: int | None = None
    last_event_id: str | None = None


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
