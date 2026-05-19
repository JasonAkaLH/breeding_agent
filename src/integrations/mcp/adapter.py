from __future__ import annotations

import inspect
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from .client import MCPClientError
from .protocol import MCPNegotiatedSession

_SAFE_METADATA_KEYS = frozenset(
    {
        "server_id",
        "status_code",
        "transport",
        "transport_family",
        "requested_protocol_version",
        "negotiated_protocol_version",
        "header_names",
        "endpoint_fingerprint",
    }
)


@dataclass(slots=True, frozen=True)
class MCPAdapterDiagnostic:
    error_code: str
    error_type: str
    retriable: bool = False
    metadata_keys: tuple[str, ...] = ()


class MCPClientAdapter(Protocol):
    @property
    def server_capabilities(self) -> Mapping[str, Any]:
        ...

    @property
    def negotiated_session(self) -> MCPNegotiatedSession | None:
        ...

    async def initialize(self) -> MCPNegotiatedSession:
        ...

    async def list_tools(self) -> list[Mapping[str, Any]]:
        ...

    async def call_tool(self, tool_name: str, arguments: Mapping[str, Any], **kwargs: Any) -> Mapping[str, Any]:
        ...

    async def close(self) -> None:
        ...

    def diagnostics(self) -> tuple[MCPAdapterDiagnostic, ...]:
        ...


class PythonLegacyMCPClientAdapter:
    """Adapter wrapper for the existing Python MCPClient implementation."""

    def __init__(self, client: Any) -> None:
        self._client = client
        self._diagnostics: list[MCPAdapterDiagnostic] = []

    @property
    def server_capabilities(self) -> Mapping[str, Any]:
        value = getattr(self._client, "server_capabilities", {})
        return dict(value) if isinstance(value, Mapping) else {}

    @property
    def negotiated_session(self) -> MCPNegotiatedSession | None:
        value = getattr(self._client, "negotiated_session", None)
        return value if isinstance(value, MCPNegotiatedSession) else None

    async def initialize(self) -> MCPNegotiatedSession:
        try:
            await self._client.initialize()
        except MCPClientError as exc:
            self._record_error(exc)
            raise
        session = self.negotiated_session
        if session is None:
            raise MCPClientError("MCP adapter did not receive a negotiated session.", code="mcp_adapter_session_missing")
        return session

    async def list_tools(self) -> list[Mapping[str, Any]]:
        try:
            tools = await self._client.list_tools()
        except MCPClientError as exc:
            self._record_error(exc)
            raise
        return [dict(tool) for tool in tools if isinstance(tool, Mapping)]

    async def call_tool(self, tool_name: str, arguments: Mapping[str, Any], **kwargs: Any) -> Mapping[str, Any]:
        try:
            result = await self._client.call_tool(tool_name, arguments, **kwargs)
        except MCPClientError as exc:
            self._record_error(exc)
            raise
        return dict(result) if isinstance(result, Mapping) else {"value": result}

    async def close(self) -> None:
        close = getattr(self._client, "close", None)
        if callable(close):
            result = close()
            if inspect.isawaitable(result):
                await result

    def diagnostics(self) -> tuple[MCPAdapterDiagnostic, ...]:
        return tuple(self._diagnostics)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    def _record_error(self, exc: MCPClientError) -> None:
        safe_keys = tuple(sorted(str(key) for key in exc.metadata if str(key) in _SAFE_METADATA_KEYS))
        self._diagnostics.append(
            MCPAdapterDiagnostic(
                error_code=exc.mcp_error_code,
                error_type=type(exc).__name__,
                retriable=exc.retriable,
                metadata_keys=safe_keys,
            )
        )


__all__ = ["MCPAdapterDiagnostic", "MCPClientAdapter", "PythonLegacyMCPClientAdapter"]
