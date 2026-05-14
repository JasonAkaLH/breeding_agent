from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .protocol import JSONRPC_VERSION, MCP_PROTOCOL_VERSION, MCPTransport, MCPStreamEvent, json_rpc_error, json_rpc_result, json_rpc_message_kind


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
        self._last_stream_notifications: tuple[Mapping[str, Any], ...] = ()

    @property
    def initialized(self) -> bool:
        return self._state == "initialized"

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def last_sse_retry_ms(self) -> int | None:
        return self._last_sse_retry_ms

    @property
    def last_stream_notifications(self) -> tuple[Mapping[str, Any], ...]:
        return self._last_stream_notifications

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
        self._last_stream_notifications = await self._handle_stream_events(response.sse_events, expected_response_id=request_id)
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

    async def send_request(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        request_registered_callback: Callable[[str | int], None] | None = None,
    ) -> Mapping[str, Any]:
        if method != "initialize" and not self.initialized:
            await self.initialize()
        try:
            return await self._send_request_once(method, params, request_registered_callback=request_registered_callback)
        except MCPClientError as exc:
            if exc.mcp_error_code != "mcp_session_expired" or method == "tools/call":
                raise
            await self._reinitialize_after_session_expiry()
            return await self._send_request_once(method, params, request_registered_callback=request_registered_callback)

    async def _send_request_once(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        request_registered_callback: Callable[[str | int], None] | None = None,
    ) -> Mapping[str, Any]:
        request_id = self._next_request_id()
        if request_registered_callback is not None:
            request_registered_callback(request_id)
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
        self._last_stream_notifications = await self._handle_stream_events(response.sse_events, expected_response_id=request_id)
        message = self._require_message(response.message, request_id=request_id)
        result = self._result_or_raise(message)
        if isinstance(result, Mapping):
            return dict(result)
        return {"value": result}

    async def _reinitialize_after_session_expiry(self) -> None:
        self._state = "new"
        self._session_id = None
        self._last_event_id = None
        self._last_sse_retry_ms = None
        self._initialize_result = None
        await self.initialize()

    async def send_notification(self, method: str, params: Mapping[str, Any] | None = None) -> None:
        response = await self._transport.send(
            {"jsonrpc": JSONRPC_VERSION, "method": method, "params": dict(params or {})},
            protocol_version=self._protocol_version,
            session_id=self._session_id,
            timeout_seconds=self._timeout_seconds,
            last_event_id=self._last_event_id,
        )
        self._capture_transport_metadata(response.headers, response.last_event_id, response.sse_retry_ms)
        self._last_stream_notifications = await self._handle_stream_events(response.sse_events, expected_response_id=None)

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

    @property
    def server_capabilities(self) -> Mapping[str, Any]:
        return dict(self._initialize_result.server_capabilities) if self._initialize_result is not None else {}

    async def call_tool(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        task_augmented: bool = False,
        progress_token: str | int | None = None,
        task_ttl_ms: int | None = None,
        request_registered_callback: Callable[[str | int], None] | None = None,
    ) -> Mapping[str, Any]:
        params: dict[str, Any] = {"name": tool_name, "arguments": dict(arguments)}
        if task_augmented:
            params["task"] = {"ttl": int(task_ttl_ms or 60000)}
            if progress_token is not None:
                params["_meta"] = {"progressToken": progress_token}
        return await self.send_request("tools/call", params, request_registered_callback=request_registered_callback)

    async def tasks_get(self, task_id: str) -> Mapping[str, Any]:
        return await self.send_request("tasks/get", {"taskId": task_id})

    async def tasks_result(self, task_id: str) -> Mapping[str, Any]:
        return await self.send_request("tasks/result", {"taskId": task_id})

    async def tasks_list(self, cursor: str | None = None) -> Mapping[str, Any]:
        params = {"cursor": cursor} if cursor else {}
        return await self.send_request("tasks/list", params)

    async def tasks_cancel(self, task_id: str, *, reason: str = "") -> Mapping[str, Any]:
        params: dict[str, Any] = {"taskId": task_id}
        if reason:
            params["reason"] = reason
        return await self.send_request("tasks/cancel", params)

    async def cancel_request(self, request_id: str | int, *, reason: str = "") -> None:
        params: dict[str, Any] = {"requestId": request_id}
        if reason:
            params["reason"] = reason
        await self.send_notification("notifications/cancelled", params)

    async def send_response(self, request_id: str | int | None, result: Mapping[str, Any] | None = None) -> None:
        response = await self._transport.send(
            json_rpc_result(request_id=request_id, result=result),
            protocol_version=self._protocol_version,
            session_id=self._session_id,
            timeout_seconds=self._timeout_seconds,
            last_event_id=self._last_event_id,
        )
        self._capture_transport_metadata(response.headers, response.last_event_id, response.sse_retry_ms)

    async def send_error_response(self, request_id: str | int | None, *, code: int, message: str) -> None:
        response = await self._transport.send(
            json_rpc_error(request_id=request_id, code=code, message=message),
            protocol_version=self._protocol_version,
            session_id=self._session_id,
            timeout_seconds=self._timeout_seconds,
            last_event_id=self._last_event_id,
        )
        self._capture_transport_metadata(response.headers, response.last_event_id, response.sse_retry_ms)

    async def open_server_stream(self) -> MCPTransportResponse:
        if not self.initialized:
            await self.initialize()
        get_stream = getattr(self._transport, "get_stream", None)
        if get_stream is None:
            raise MCPProtocolError("MCP transport does not support GET server stream.")
        response = await get_stream(
            protocol_version=self._protocol_version,
            session_id=self._session_id,
            timeout_seconds=self._timeout_seconds,
            last_event_id=self._last_event_id,
        )
        self._capture_transport_metadata(response.headers, response.last_event_id, response.sse_retry_ms)
        self._last_stream_notifications = await self._handle_stream_events(response.sse_events, expected_response_id=None)
        return response

    async def close(self) -> None:
        if self._state == "closed":
            return
        self._state = "closed"
        await self._transport.close()

    async def _handle_stream_events(self, events: tuple[MCPStreamEvent, ...], *, expected_response_id: str | int | None) -> tuple[Mapping[str, Any], ...]:
        notifications: list[Mapping[str, Any]] = []
        for event in events:
            message = event.message
            if message is None:
                continue
            try:
                kind = json_rpc_message_kind(message)
            except ValueError as exc:
                raise MCPProtocolError(str(exc)) from exc
            if kind == "request":
                if message.get("method") == "ping":
                    await self.send_response(message.get("id"), {})
                else:
                    await self.send_error_response(
                        message.get("id"),
                        code=MCPUnsupportedClientRequest.ERROR_CODE,
                        message=f"Unsupported MCP client request method: {message.get('method') or '<unknown>'}",
                    )
            elif kind == "notification":
                notifications.append(dict(message))
            elif kind == "response" and expected_response_id is not None and message.get("id") != expected_response_id:
                raise MCPProtocolError("MCP response id does not match request id.")
        return tuple(notifications)

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
        if message.get("method") is not None:
            raise MCPProtocolError("MCP request expected a JSON-RPC response, got server request or notification.")
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
