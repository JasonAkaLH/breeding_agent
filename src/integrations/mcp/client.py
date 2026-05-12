from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .protocol import JSONRPC_VERSION, MCP_PROTOCOL_VERSION, MCPTransport, json_rpc_error


class MCPClientError(RuntimeError):
    def __init__(self, message: str, *, code: str = "mcp_client_error", retriable: bool = False, metadata: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.mcp_error_code = code
        self.retriable = retriable
        self.metadata = dict(metadata or {})


class MCPProtocolError(MCPClientError):
    def __init__(self, message: str, *, metadata: Mapping[str, Any] | None = None) -> None:
        super().__init__(message, code="mcp_protocol_error", retriable=False, metadata=metadata)


class MCPRemoteError(MCPClientError):
    def __init__(self, message: str, *, remote_code: int | str | None = None, retriable: bool = False, metadata: Mapping[str, Any] | None = None) -> None:
        merged = {**dict(metadata or {}), "remote_code": remote_code}
        super().__init__(message, code="mcp_remote_error", retriable=retriable, metadata=merged)


class MCPAuthRequiredError(MCPClientError):
    def __init__(self, message: str = "MCP authorization is required.", *, scope_required: bool = False, metadata: Mapping[str, Any] | None = None) -> None:
        super().__init__(
            message,
            code="mcp_scope_required" if scope_required else "mcp_auth_required",
            retriable=False,
            metadata=metadata,
        )


class MCPUnsupportedClientRequest:
    ERROR_CODE = -32601


@dataclass(slots=True)
class MCPInitializeResult:
    protocol_version: str
    server_capabilities: Mapping[str, Any]
    server_info: Mapping[str, Any]


class MCPClient:
    """Small MCP 2025-11-25 JSON-RPC client with lifecycle enforcement."""

    def __init__(
        self,
        *,
        server_id: str,
        transport: MCPTransport,
        protocol_version: str = MCP_PROTOCOL_VERSION,
        timeout_seconds: float = 20,
        client_info: Mapping[str, Any] | None = None,
        client_capabilities: Mapping[str, Any] | None = None,
    ) -> None:
        self.server_id = server_id
        self._transport = transport
        self._protocol_version = protocol_version
        self._timeout_seconds = timeout_seconds
        self._client_info = dict(client_info or {"name": "multi_agent_framework", "version": "1"})
        self._client_capabilities = self._minimal_capabilities(client_capabilities or {})
        self._state = "new"
        self._request_id = 0
        self._session_id: str | None = None
        self._last_event_id: str | None = None
        self._last_sse_retry_ms: int | None = None
        self._initialize_result: MCPInitializeResult | None = None

    @property
    def initialized(self) -> bool:
        return self._state == "initialized"

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def last_sse_retry_ms(self) -> int | None:
        return self._last_sse_retry_ms

    async def initialize(self) -> Mapping[str, Any]:
        if self.initialized and self._initialize_result is not None:
            return {
                "protocolVersion": self._initialize_result.protocol_version,
                "capabilities": dict(self._initialize_result.server_capabilities),
                "serverInfo": dict(self._initialize_result.server_info),
            }
        if self._state == "closed":
            raise MCPProtocolError("Cannot initialize a closed MCP client.")
        self._state = "initializing"
        request_id = self._next_request_id()
        response = await self._transport.send(
            {
                "jsonrpc": JSONRPC_VERSION,
                "id": request_id,
                "method": "initialize",
                "params": {
                    "protocolVersion": self._protocol_version,
                    "capabilities": self._client_capabilities,
                    "clientInfo": self._client_info,
                },
            },
            protocol_version=self._protocol_version,
            session_id=None,
            timeout_seconds=self._timeout_seconds,
            last_event_id=self._last_event_id,
        )
        self._capture_transport_metadata(response.headers, response.last_event_id, response.sse_retry_ms)
        message = self._require_message(response.message, request_id=request_id)
        result = self._result_or_raise(message)
        if not isinstance(result, Mapping):
            raise MCPProtocolError("initialize result must be an object.")
        negotiated_version = str(result.get("protocolVersion") or "").strip()
        if negotiated_version != self._protocol_version:
            self._state = "failed"
            raise MCPProtocolError(
                "Unsupported MCP protocol version negotiated.",
                metadata={"server_id": self.server_id, "negotiated_protocol_version": negotiated_version},
            )
        server_capabilities = result.get("capabilities") if isinstance(result.get("capabilities"), Mapping) else {}
        server_info = result.get("serverInfo") if isinstance(result.get("serverInfo"), Mapping) else {}
        self._initialize_result = MCPInitializeResult(
            protocol_version=negotiated_version,
            server_capabilities=dict(server_capabilities),
            server_info=dict(server_info),
        )
        await self.send_notification("notifications/initialized", {})
        self._state = "initialized"
        return dict(result)

    async def send_request(self, method: str, params: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
        if method != "initialize" and not self.initialized:
            await self.initialize()
        request_id = self._next_request_id()
        response = await self._transport.send(
            {
                "jsonrpc": JSONRPC_VERSION,
                "id": request_id,
                "method": method,
                "params": dict(params or {}),
            },
            protocol_version=self._protocol_version,
            session_id=self._session_id,
            timeout_seconds=self._timeout_seconds,
            last_event_id=self._last_event_id,
        )
        self._capture_transport_metadata(response.headers, response.last_event_id, response.sse_retry_ms)
        message = self._require_message(response.message, request_id=request_id)
        result = self._result_or_raise(message)
        if isinstance(result, Mapping):
            return dict(result)
        return {"value": result}

    async def send_notification(self, method: str, params: Mapping[str, Any] | None = None) -> None:
        response = await self._transport.send(
            {"jsonrpc": JSONRPC_VERSION, "method": method, "params": dict(params or {})},
            protocol_version=self._protocol_version,
            session_id=self._session_id,
            timeout_seconds=self._timeout_seconds,
            last_event_id=self._last_event_id,
        )
        self._capture_transport_metadata(response.headers, response.last_event_id, response.sse_retry_ms)

    async def list_tools(self) -> list[Mapping[str, Any]]:
        tools: list[Mapping[str, Any]] = []
        cursor: str | None = None
        while True:
            params = {"cursor": cursor} if cursor else {}
            result = await self.send_request("tools/list", params)
            raw_tools = result.get("tools")
            if isinstance(raw_tools, list):
                tools.extend(item for item in raw_tools if isinstance(item, Mapping))
            next_cursor = result.get("nextCursor")
            if not next_cursor:
                return tools
            cursor = str(next_cursor)

    async def call_tool(self, tool_name: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        return await self.send_request("tools/call", {"name": tool_name, "arguments": dict(arguments)})

    async def close(self) -> None:
        if self._state == "closed":
            return
        self._state = "closed"
        await self._transport.close()

    @staticmethod
    def unsupported_client_request_response(message: Mapping[str, Any]) -> dict[str, Any]:
        return json_rpc_error(
            request_id=message.get("id"),
            code=MCPUnsupportedClientRequest.ERROR_CODE,
            message=f"Unsupported MCP client request method: {message.get('method') or '<unknown>'}",
        )

    @staticmethod
    def _minimal_capabilities(configured: Mapping[str, Any]) -> dict[str, Any]:
        # Phase 1 only advertises capabilities that are explicitly implemented.
        # The PRD requires roots/sampling/elicitation/tasks to remain absent by default.
        unimplemented = {"roots", "sampling", "elicitation", "tasks"}
        return {
            str(key): value
            for key, value in configured.items()
            if str(key) not in unimplemented and value not in (False, None, {}, [])
        }

    def _next_request_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _capture_transport_metadata(self, headers: Mapping[str, str], last_event_id: str | None, sse_retry_ms: int | None) -> None:
        for key, value in headers.items():
            if key.lower() == "mcp-session-id" and value:
                self._session_id = str(value)
                break
        if last_event_id:
            self._last_event_id = last_event_id
        if sse_retry_ms is not None:
            self._last_sse_retry_ms = sse_retry_ms

    @staticmethod
    def _require_message(message: Mapping[str, Any] | None, *, request_id: int) -> Mapping[str, Any]:
        if not isinstance(message, Mapping):
            raise MCPProtocolError("MCP request expected a JSON-RPC response.")
        if message.get("jsonrpc") != JSONRPC_VERSION:
            raise MCPProtocolError("MCP response must use JSON-RPC 2.0.")
        if message.get("id") != request_id:
            raise MCPProtocolError("MCP response id does not match request id.")
        return message

    @staticmethod
    def _result_or_raise(message: Mapping[str, Any]) -> Any:
        if "error" in message:
            error = message.get("error")
            if isinstance(error, Mapping):
                raise MCPRemoteError(
                    str(error.get("message") or "MCP server returned an error."),
                    remote_code=error.get("code"),
                    retriable=_is_retriable_remote_error(error.get("code")),
                )
            raise MCPRemoteError("MCP server returned an error.")
        if "result" not in message:
            raise MCPProtocolError("MCP response must include result or error.")
        return message.get("result")


def _is_retriable_remote_error(code: Any) -> bool:
    try:
        parsed = int(code)
    except (TypeError, ValueError):
        return False
    return parsed in {-32000, -32001, -32002}
