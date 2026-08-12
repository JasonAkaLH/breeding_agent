from __future__ import annotations

import base64
import re
import secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator, Draft7Validator, SchemaError, ValidationError

from .client import MCPClientError, MCPProtocolError, MCPRemoteError
from .credentials import (
    CredentialSecurityError,
    MCPRecoveryCallContext,
    MCPRecoveryService,
)
from .protocol import (
    JSONRPC_VERSION,
    MCP_PROTOCOL_VERSION_2026_07_28,
    MCPNegotiatedSession,
    MCPRequestScopedTransport,
    MCPTransportResponse,
    MCP_TRANSPORT_STREAMABLE_HTTP,
    SUPPORTED_MCP_PROTOCOL_VERSION_ORDER,
    json_rpc_message_kind,
)

_PROTOCOL_META = "io.modelcontextprotocol/protocolVersion"
_CLIENT_INFO_META = "io.modelcontextprotocol/clientInfo"
_CLIENT_CAPABILITIES_META = "io.modelcontextprotocol/clientCapabilities"
_SERVER_INFO_META = "io.modelcontextprotocol/serverInfo"
_TASKS_EXTENSION = "io.modelcontextprotocol/tasks"
_HEADER_TOKEN_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_BASE64_SENTINEL_RE = re.compile(r"^=\?base64\?.*\?=$", re.DOTALL)
_MAX_SAFE_INTEGER = (1 << 53) - 1
_PROTECTED_HEADERS = frozenset(
    {
        "accept",
        "authorization",
        "content-length",
        "content-type",
        "cookie",
        "host",
        "mcp-protocol-version",
        "mcp-method",
        "mcp-name",
        "mcp-session-id",
        "last-event-id",
        "origin",
        "proxy-authorization",
    }
)
_TASK_STATUSES = frozenset({"working", "input_required", "completed", "failed", "cancelled"})
_TERMINAL_TASK_STATUSES = frozenset({"completed", "failed", "cancelled"})


@dataclass(slots=True, frozen=True)
class MCPListCacheHint:
    ttl_ms: int
    cache_scope: str


@dataclass(slots=True, frozen=True)
class MCPDiscoverResult:
    supported_versions: tuple[str, ...]
    capabilities: Mapping[str, Any]
    server_info: Mapping[str, Any]
    instructions: str | None
    cache_hint: MCPListCacheHint


@dataclass(slots=True, frozen=True)
class MCPToolCatalogPage:
    tools: tuple[Mapping[str, Any], ...]
    next_cursor: str | None
    cache_hint: MCPListCacheHint


@dataclass(slots=True, frozen=True)
class MCPCompletedOutcome:
    result: Mapping[str, Any]
    kind: str = "completed"


@dataclass(slots=True, frozen=True)
class MCPInputRequiredOutcome:
    input_requests: Mapping[str, Mapping[str, Any]]
    sealed_request_state_ref: str | None
    kind: str = "input_required"


@dataclass(slots=True, frozen=True)
class MCPTaskCreatedOutcome:
    safe_remote_task_ref: str
    status: str
    ttl_ms: int | None
    poll_interval_ms: int | None
    kind: str = "task_created"


@dataclass(slots=True, frozen=True)
class MCPTaskState:
    safe_remote_task_ref: str
    status: str
    terminal: bool
    ttl_ms: int | None = None
    poll_interval_ms: int | None = None
    status_message: str | None = None
    input_requests: Mapping[str, Mapping[str, Any]] | None = None
    result: Mapping[str, Any] | None = None
    error: Mapping[str, Any] | None = None


MCPCallOutcome = MCPCompletedOutcome | MCPInputRequiredOutcome | MCPTaskCreatedOutcome


class MCPUnsupportedProtocolVersionError(MCPProtocolError):
    def __init__(self, *, supported_versions: Sequence[str], requested_version: str, request_method: str) -> None:
        super().__init__(
            "MCP server does not support the requested protocol version.",
            metadata={
                "requested_protocol_version": requested_version,
                "supported_protocol_versions": tuple(supported_versions),
            },
        )
        self.supported_versions = tuple(supported_versions)
        self.requested_version = requested_version
        self.request_method = request_method


class MCPMethodNotFoundError(MCPProtocolError):
    def __init__(self, message: str, *, request_method: str) -> None:
        super().__init__(message)
        self.request_method = request_method


class MCP2026Adapter:
    """Stateless MCP 2026-07-28 adapter.

    The adapter owns wire metadata, routing headers, schema header annotations,
    MRTR/task normalization, and opaque reference lifetimes. The transport owns
    one-POST-per-request JSON/SSE I/O and request cancellation by stream close.
    """

    protocol_version = MCP_PROTOCOL_VERSION_2026_07_28

    def __init__(
        self,
        *,
        server_id: str,
        transport: MCPRequestScopedTransport,
        timeout_seconds: float | None = 20,
        client_info: Mapping[str, Any] | None = None,
        enable_elicitation: bool = False,
        enable_tasks: bool = False,
        safe_ref_factory: Callable[[str], str] | None = None,
        recovery_service: MCPRecoveryService | None = None,
        recovery_only: bool = False,
    ) -> None:
        self.server_id = server_id
        self._transport = transport
        self._timeout_seconds = timeout_seconds
        self._client_info = self._validate_client_info(client_info or {"name": "breeding_agent", "version": "1"})
        self._client_capabilities = self._build_client_capabilities(
            enable_elicitation=enable_elicitation,
            enable_tasks=enable_tasks,
        )
        self._enable_elicitation = bool(enable_elicitation)
        self._enable_tasks = bool(enable_tasks)
        self._safe_ref_factory = safe_ref_factory or (lambda prefix: f"{prefix}:{secrets.token_urlsafe(24)}")
        self._recovery_service = recovery_service
        self._recovery_only = bool(recovery_only)
        self._request_id = 0
        self._closed = False
        self._discover_result: MCPDiscoverResult | None = None
        self._tool_schemas: dict[str, Mapping[str, Any]] = {}
        self._sealed_request_states: dict[str, str] = {}
        self._remote_task_ids: dict[str, str] = {}
        self._last_stream_notifications: tuple[Mapping[str, Any], ...] = ()

    @property
    def server_capabilities(self) -> Mapping[str, Any]:
        return dict(self._discover_result.capabilities) if self._discover_result is not None else {}

    @property
    def supports_durable_recovery_context(self) -> bool:
        return self._recovery_service is not None

    @property
    def negotiated_session(self) -> MCPNegotiatedSession | None:
        if self._discover_result is None:
            return None
        return MCPNegotiatedSession(
            server_id=self.server_id,
            requested_protocol_version=self.protocol_version,
            negotiated_protocol_version=self.protocol_version,
            transport_family=MCP_TRANSPORT_STREAMABLE_HTTP,
            server_capabilities=self.server_capabilities,
            server_info=dict(self._discover_result.server_info),
            pinned_protocol_version=True,
        )

    @property
    def last_stream_notifications(self) -> tuple[Mapping[str, Any], ...]:
        return self._last_stream_notifications

    async def initialize(self) -> MCPNegotiatedSession:
        if self._discover_result is None:
            await self.discover()
        session = self.negotiated_session
        if session is None:
            raise MCPProtocolError("MCP 2026 adapter did not complete discovery.")
        return session

    async def discover(self) -> MCPDiscoverResult:
        result = await self._send_request("server/discover", {})
        if result.get("resultType") != "complete":
            raise MCPProtocolError("server/discover resultType must be complete.")
        raw_versions = result.get("supportedVersions")
        if not isinstance(raw_versions, list) or not raw_versions or not all(isinstance(item, str) and item for item in raw_versions):
            raise MCPProtocolError("server/discover supportedVersions must be a non-empty string array.")
        capabilities = result.get("capabilities")
        if not isinstance(capabilities, Mapping):
            raise MCPProtocolError("server/discover capabilities must be an object.")
        if self.protocol_version not in raw_versions:
            raise MCPUnsupportedProtocolVersionError(
                supported_versions=tuple(raw_versions),
                requested_version=self.protocol_version,
                request_method="server/discover",
            )
        metadata = result.get("_meta")
        server_info = metadata.get(_SERVER_INFO_META) if isinstance(metadata, Mapping) else {}
        if not isinstance(server_info, Mapping):
            raise MCPProtocolError("server/discover serverInfo must be an object.")
        discovered = MCPDiscoverResult(
            supported_versions=tuple(raw_versions),
            capabilities=dict(capabilities),
            server_info=dict(server_info),
            instructions=str(result["instructions"]) if isinstance(result.get("instructions"), str) else None,
            cache_hint=_parse_cache_hint(result),
        )
        self._discover_result = discovered
        return discovered

    async def list_tools_page(self, cursor: str | None = None) -> MCPToolCatalogPage:
        self._require_tools_capability()
        params = {"cursor": cursor} if cursor else {}
        result = await self._send_request("tools/list", params)
        if result.get("resultType") != "complete":
            raise MCPProtocolError("tools/list resultType must be complete.")
        raw_tools = result.get("tools")
        if not isinstance(raw_tools, list):
            raise MCPProtocolError("tools/list tools must be an array.")
        tools: list[Mapping[str, Any]] = []
        for raw_tool in raw_tools:
            normalized = _normalize_tool(raw_tool)
            if normalized is None:
                continue
            name = str(normalized["name"])
            self._tool_schemas[name] = normalized["inputSchema"]
            tools.append(normalized)
        next_cursor = result.get("nextCursor")
        if next_cursor is not None and (not isinstance(next_cursor, str) or not next_cursor):
            raise MCPProtocolError("tools/list nextCursor must be a non-empty string when present.")
        return MCPToolCatalogPage(tuple(tools), next_cursor, _parse_cache_hint(result))

    async def list_tools(self) -> list[Mapping[str, Any]]:
        tools: list[Mapping[str, Any]] = []
        tool_names: set[str] = set()
        cursor: str | None = None
        seen_cursors: set[str] = set()
        while True:
            page = await self.list_tools_page(cursor)
            for tool in page.tools:
                name = str(tool["name"])
                if name in tool_names:
                    raise MCPProtocolError("tools/list returned a duplicate tool name.")
                tool_names.add(name)
                tools.append(tool)
            if page.next_cursor is None:
                return tools
            if page.next_cursor in seen_cursors:
                raise MCPProtocolError("tools/list pagination cursor repeated.")
            seen_cursors.add(page.next_cursor)
            cursor = page.next_cursor

    async def call_tool(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        input_responses: Mapping[str, Any] | None = None,
        sealed_request_state_ref: str | None = None,
        request_registered_callback: Callable[[str | int], None] | None = None,
        result_sink: Any | None = None,
        recovery_context: MCPRecoveryCallContext | None = None,
        **_kwargs: Any,
    ) -> MCPCallOutcome:
        self._require_tools_capability()
        schema = self._tool_schemas.get(tool_name)
        if schema is None:
            raise MCPProtocolError("Tool must be listed before it can be called with MCP 2026 header routing.")
        _validate_arguments(schema, arguments)
        params: dict[str, Any] = {"name": tool_name, "arguments": dict(arguments)}
        if input_responses is not None:
            if not isinstance(input_responses, Mapping):
                raise MCPProtocolError("inputResponses must be an object.")
            params["inputResponses"] = dict(input_responses)
            if sealed_request_state_ref is not None:
                params["requestState"] = await self._resolve_request_state_for_call(
                    sealed_request_state_ref,
                    tool_name=tool_name,
                    arguments=arguments,
                    recovery_context=recovery_context,
                )
        headers = _tool_parameter_headers(schema, arguments)
        result = await self._send_request(
            "tools/call",
            params,
            extra_headers=headers,
            request_registered_callback=request_registered_callback,
            result_sink=result_sink,
        )
        return await self._normalize_call_outcome(
            result,
            tool_name=tool_name,
            arguments=arguments,
            recovery_context=recovery_context,
        )

    async def tasks_get(
        self,
        safe_remote_task_ref: str,
        *,
        recovery_context: MCPRecoveryCallContext | None = None,
    ) -> MCPTaskState:
        self._require_tasks_capability()
        raw_task_id = await self._resolve_task_id_for_context(
            safe_remote_task_ref,
            recovery_context=recovery_context,
        )
        result = await self._send_request("tasks/get", {"taskId": raw_task_id})
        return self._normalize_task_state(
            result,
            safe_remote_task_ref=safe_remote_task_ref,
            expected_raw_task_id=raw_task_id,
        )

    async def tasks_update(
        self,
        safe_remote_task_ref: str,
        input_responses: Mapping[str, Any],
        *,
        recovery_context: MCPRecoveryCallContext | None = None,
    ) -> MCPCompletedOutcome:
        self._require_tasks_capability()
        if not isinstance(input_responses, Mapping):
            raise MCPProtocolError("inputResponses must be an object.")
        result = await self._send_request(
            "tasks/update",
            {
                "taskId": await self._resolve_task_id_for_context(
                    safe_remote_task_ref,
                    recovery_context=recovery_context,
                ),
                "inputResponses": dict(input_responses),
            },
        )
        return _complete_ack(result, "tasks/update")

    async def tasks_cancel(
        self,
        safe_remote_task_ref: str,
        *,
        reason: str = "",
        recovery_context: MCPRecoveryCallContext | None = None,
    ) -> MCPCompletedOutcome:
        self._require_tasks_capability()
        params: dict[str, Any] = {
            "taskId": await self._resolve_task_id_for_context(
                safe_remote_task_ref,
                recovery_context=recovery_context,
            )
        }
        result = await self._send_request("tasks/cancel", params)
        return _complete_ack(result, "tasks/cancel")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._sealed_request_states.clear()
        self._remote_task_ids.clear()
        self._tool_schemas.clear()
        await self._transport.close()

    def diagnostics(self) -> tuple[Any, ...]:
        return ()

    async def _send_request(
        self,
        method: str,
        params: Mapping[str, Any],
        *,
        extra_headers: Mapping[str, str] | None = None,
        request_registered_callback: Callable[[str | int], None] | None = None,
        result_sink: Any | None = None,
    ) -> Mapping[str, Any]:
        if self._closed:
            raise MCPProtocolError("Cannot use a closed MCP 2026 adapter.")
        if self._recovery_only and method not in {
            "tasks/get",
            "tasks/update",
            "tasks/cancel",
        }:
            raise MCPProtocolError(
                "MCP recovery-only client permits only task query/control methods."
            )
        request_id = self._next_request_id()
        if request_registered_callback is not None:
            request_registered_callback(request_id)
        request_params = dict(params)
        existing_meta = request_params.get("_meta")
        if existing_meta is not None and not isinstance(existing_meta, Mapping):
            raise MCPProtocolError("MCP request _meta must be an object.")
        request_params["_meta"] = {
            **dict(existing_meta or {}),
            _PROTOCOL_META: self.protocol_version,
            _CLIENT_INFO_META: dict(self._client_info),
            _CLIENT_CAPABILITIES_META: dict(self._client_capabilities),
        }
        message = {"jsonrpc": JSONRPC_VERSION, "id": request_id, "method": method, "params": request_params}
        headers = {
            "MCP-Protocol-Version": self.protocol_version,
            "Mcp-Method": method,
        }
        if method in {"tools/call", "prompts/get"}:
            name = request_params.get("name")
        elif method == "resources/read":
            name = request_params.get("uri")
        elif method in {"tasks/get", "tasks/update", "tasks/cancel"}:
            name = request_params.get("taskId")
        else:
            name = None
        if name is not None:
            headers["Mcp-Name"] = encode_mcp_header_value(str(name))
        for key, value in dict(extra_headers or {}).items():
            lowered = str(key).lower()
            if lowered in _PROTECTED_HEADERS or not lowered.startswith("mcp-param-"):
                raise MCPProtocolError("MCP custom header attempted to override a protected header.")
            if any(existing.lower() == lowered for existing in headers):
                raise MCPProtocolError("MCP custom header name is duplicated.")
            headers[str(key)] = str(value)
        try:
            sender = (
                getattr(self._transport, "send_streaming")
                if result_sink is not None and hasattr(self._transport, "send_streaming")
                else self._transport.send
            )
            send_kwargs: dict[str, Any] = {
                "protocol_version": self.protocol_version,
                "request_headers": headers,
                "timeout_seconds": None if method == "tools/call" else self._timeout_seconds,
            }
            if result_sink is not None and hasattr(self._transport, "send_streaming"):
                send_kwargs["result_sink"] = result_sink
            response = await sender(
                message,
                **send_kwargs,
            )
        except (MCPClientError, MCPProtocolError):
            raise
        except Exception as exc:
            raise MCPClientError("MCP 2026 request transport failed.", code="mcp_transport_error", retriable=True) from exc
        return await self._parse_response(response, request_id=request_id, request_method=method)

    async def _parse_response(
        self,
        response: MCPTransportResponse,
        *,
        request_id: int,
        request_method: str,
    ) -> Mapping[str, Any]:
        if response.last_event_id is not None:
            raise MCPProtocolError("MCP 2026 request-scoped SSE must not expose Last-Event-ID.")
        message = response.message
        notifications: list[Mapping[str, Any]] = []
        final_from_stream: Mapping[str, Any] | None = None
        for event in response.sse_events:
            if event.event_id is not None:
                raise MCPProtocolError("MCP 2026 request-scoped SSE events must not carry event ids.")
            candidate = event.message
            if candidate is None:
                continue
            try:
                kind = json_rpc_message_kind(candidate)
            except ValueError as exc:
                raise MCPProtocolError(str(exc)) from exc
            if kind == "notification":
                notifications.append(dict(candidate))
                continue
            if kind == "request":
                raise MCPProtocolError("MCP 2026 SSE response must not contain server-to-client requests.")
            if candidate.get("id") != request_id:
                raise MCPProtocolError("MCP response id does not match request id.")
            if final_from_stream is not None:
                raise MCPProtocolError("MCP 2026 SSE response contained multiple final responses.")
            final_from_stream = candidate
        self._last_stream_notifications = tuple(notifications)
        if message is not None and final_from_stream is not None and dict(message) != dict(final_from_stream):
            raise MCPProtocolError("MCP 2026 transport exposed conflicting JSON and SSE final responses.")
        final = final_from_stream or message
        if not isinstance(final, Mapping):
            raise MCPProtocolError("MCP 2026 request expected a JSON-RPC response.")
        if final.get("jsonrpc") != JSONRPC_VERSION or final.get("id") != request_id or final.get("method") is not None:
            raise MCPProtocolError("MCP 2026 response is not a matching JSON-RPC response.")
        if "error" in final:
            self._raise_remote_error(final["error"], request_method=request_method)
        result = final.get("result")
        if not isinstance(result, Mapping):
            raise MCPProtocolError("MCP 2026 result must be an object.")
        return dict(result)

    def _raise_remote_error(self, raw_error: Any, *, request_method: str) -> None:
        if not isinstance(raw_error, Mapping):
            raise MCPRemoteError("MCP server returned an error.")
        code = raw_error.get("code")
        data = raw_error.get("data")
        if code == -32022 and isinstance(data, Mapping):
            supported = data.get("supported")
            requested = data.get("requested")
            if (
                isinstance(supported, list)
                and all(isinstance(item, str) and item for item in supported)
                and requested == self.protocol_version
            ):
                raise MCPUnsupportedProtocolVersionError(
                    supported_versions=tuple(supported),
                    requested_version=self.protocol_version,
                    request_method=request_method,
                )
        if code == -32601:
            raise MCPMethodNotFoundError(
                f"MCP server does not implement {request_method}.",
                request_method=request_method,
            )
        raise MCPRemoteError(str(raw_error.get("message") or "MCP server returned an error."), remote_code=code)

    async def _normalize_call_outcome(
        self,
        result: Mapping[str, Any],
        *,
        tool_name: str,
        arguments: Mapping[str, Any],
        recovery_context: MCPRecoveryCallContext | None,
    ) -> MCPCallOutcome:
        if isinstance(result.get("_mcpResultRef"), Mapping):
            return MCPCompletedOutcome(dict(result))
        result_type = result.get("resultType")
        if result_type == "complete":
            return MCPCompletedOutcome(dict(result))
        if result_type == "input_required":
            input_requests = _validate_input_requests(
                result.get("inputRequests"),
                enable_elicitation=self._enable_elicitation,
                require_nonempty=False,
            )
            request_state = result.get("requestState")
            if request_state is not None and not isinstance(request_state, str):
                raise MCPProtocolError("InputRequiredResult requestState must be a string when present.")
            safe_ref = None
            if request_state is not None:
                safe_ref = self._safe_ref_factory("mcp-request-state")
                service = self._require_recovery_service(recovery_context)
                try:
                    await service.save_request_state(
                        recovery_context,
                        server_id=self.server_id,
                        protocol_version=self.protocol_version,
                        sealed_state_ref=safe_ref,
                        request_state=request_state,
                        tool_name=tool_name,
                        arguments=arguments,
                    )
                except CredentialSecurityError as exc:
                    raise MCPProtocolError("MCP request state could not be sealed.") from exc
                self._sealed_request_states[safe_ref] = request_state
            return MCPInputRequiredOutcome(input_requests=input_requests, sealed_request_state_ref=safe_ref)
        if result_type == "task":
            self._require_tasks_capability()
            task = result
            raw_task_id = _non_empty_string(task.get("taskId"), "CreateTaskResult taskId")
            status = _task_status(task.get("status"))
            _non_empty_string(task.get("createdAt"), "CreateTaskResult createdAt")
            _non_empty_string(task.get("lastUpdatedAt"), "CreateTaskResult lastUpdatedAt")
            ttl_ms = _optional_non_negative_int(task.get("ttlMs"), "CreateTaskResult ttlMs")
            poll_interval_ms = _optional_non_negative_int(task.get("pollIntervalMs"), "CreateTaskResult pollIntervalMs")
            safe_ref = self._safe_ref_factory("mcp-task")
            service = self._require_recovery_service(recovery_context)
            try:
                await service.save_remote_task(
                    recovery_context,
                    server_id=self.server_id,
                    protocol_version=self.protocol_version,
                    safe_remote_task_ref=safe_ref,
                    remote_task_id=raw_task_id,
                    status=status,
                    poll_interval_ms=poll_interval_ms,
                )
            except CredentialSecurityError as exc:
                raise MCPProtocolError("MCP remote task could not be bound.") from exc
            self._remote_task_ids[safe_ref] = raw_task_id
            return MCPTaskCreatedOutcome(safe_ref, status, ttl_ms, poll_interval_ms)
        raise MCPProtocolError("MCP 2026 resultType must be complete, input_required, or task.")

    def _normalize_task_state(
        self,
        result: Mapping[str, Any],
        *,
        safe_remote_task_ref: str,
        expected_raw_task_id: str,
    ) -> MCPTaskState:
        if result.get("resultType") != "complete":
            raise MCPProtocolError("tasks/get resultType must be complete.")
        task = result.get("task") if isinstance(result.get("task"), Mapping) else result
        raw_task_id = _non_empty_string(task.get("taskId"), "Task taskId")
        if raw_task_id != expected_raw_task_id:
            raise MCPProtocolError("Task response taskId does not match the requested task.")
        status = _task_status(task.get("status"))
        _non_empty_string(task.get("createdAt"), "Task createdAt")
        _non_empty_string(task.get("lastUpdatedAt"), "Task lastUpdatedAt")
        input_requests = None
        if status == "input_required":
            input_requests = _validate_input_requests(
                task.get("inputRequests"),
                enable_elicitation=self._enable_elicitation,
                require_nonempty=True,
            )
        normalized_result = task.get("result")
        normalized_error = task.get("error")
        if normalized_result is not None and not isinstance(normalized_result, Mapping):
            raise MCPProtocolError("Task result must be an object when present.")
        if normalized_error is not None and not isinstance(normalized_error, Mapping):
            raise MCPProtocolError("Task error must be an object when present.")
        return MCPTaskState(
            safe_remote_task_ref=safe_remote_task_ref,
            status=status,
            terminal=status in _TERMINAL_TASK_STATUSES,
            ttl_ms=_optional_non_negative_int(task.get("ttlMs"), "Task ttlMs"),
            poll_interval_ms=_optional_non_negative_int(task.get("pollIntervalMs"), "Task pollIntervalMs"),
            status_message=str(task["statusMessage"]) if isinstance(task.get("statusMessage"), str) else None,
            input_requests=input_requests,
            result=dict(normalized_result) if isinstance(normalized_result, Mapping) else None,
            error=dict(normalized_error) if isinstance(normalized_error, Mapping) else None,
        )

    def _require_tools_capability(self) -> None:
        if self._discover_result is None:
            raise MCPProtocolError("server/discover must complete before tools requests.")
        if not isinstance(self._discover_result.capabilities.get("tools"), Mapping):
            raise MCPProtocolError("MCP server did not declare the tools capability.")

    def _require_tasks_capability(self) -> None:
        if not self._enable_tasks:
            raise MCPProtocolError("MCP Tasks extension is not enabled by this client.")
        if self._recovery_only:
            return
        extensions = self.server_capabilities.get("extensions")
        if not isinstance(extensions, Mapping) or not isinstance(extensions.get(_TASKS_EXTENSION), Mapping):
            raise MCPProtocolError("MCP server did not declare the Tasks extension.")

    def _resolve_request_state(self, safe_ref: str) -> str:
        try:
            return self._sealed_request_states[safe_ref]
        except KeyError as exc:
            raise MCPProtocolError("Unknown or expired sealed request state reference.") from exc

    def _resolve_task_id(self, safe_ref: str) -> str:
        try:
            return self._remote_task_ids[safe_ref]
        except KeyError as exc:
            raise MCPProtocolError("Unknown or expired remote task reference.") from exc

    async def _resolve_request_state_for_call(
        self,
        safe_ref: str,
        *,
        tool_name: str,
        arguments: Mapping[str, Any],
        recovery_context: MCPRecoveryCallContext | None,
    ) -> str:
        service = self._require_recovery_service(recovery_context)
        try:
            return await service.resolve_request_state(
                recovery_context,
                server_id=self.server_id,
                protocol_version=self.protocol_version,
                sealed_state_ref=safe_ref,
                tool_name=tool_name,
                arguments=arguments,
            )
        except CredentialSecurityError as exc:
            raise MCPProtocolError("Unknown or expired sealed request state reference.") from exc

    async def _resolve_task_id_for_context(
        self,
        safe_ref: str,
        *,
        recovery_context: MCPRecoveryCallContext | None,
    ) -> str:
        service = self._require_recovery_service(recovery_context)
        try:
            return await service.resolve_remote_task_id(
                recovery_context,
                server_id=self.server_id,
                protocol_version=self.protocol_version,
                safe_remote_task_ref=safe_ref,
            )
        except CredentialSecurityError as exc:
            raise MCPProtocolError("Unknown or expired remote task reference.") from exc

    def _require_recovery_service(
        self,
        recovery_context: MCPRecoveryCallContext | None,
    ) -> MCPRecoveryService:
        if self._recovery_service is None:
            raise MCPProtocolError("Durable MCP recovery is unavailable.")
        if recovery_context is None:
            raise MCPProtocolError("MCP recovery context is required.")
        return self._recovery_service

    def _next_request_id(self) -> int:
        self._request_id += 1
        return self._request_id

    @staticmethod
    def _validate_client_info(value: Mapping[str, Any]) -> Mapping[str, Any]:
        name = value.get("name")
        version = value.get("version")
        if not isinstance(name, str) or not name or not isinstance(version, str) or not version:
            raise ValueError("MCP 2026 clientInfo requires non-empty name and version strings.")
        return dict(value)

    @staticmethod
    def _build_client_capabilities(*, enable_elicitation: bool, enable_tasks: bool) -> Mapping[str, Any]:
        capabilities: dict[str, Any] = {}
        if enable_elicitation:
            capabilities["elicitation"] = {"form": {}}
        if enable_tasks:
            capabilities["extensions"] = {_TASKS_EXTENSION: {}}
        return capabilities


def encode_mcp_header_value(value: str) -> str:
    plain_ascii = all(character == "\t" or 0x20 <= ord(character) <= 0x7E for character in value)
    if plain_ascii and value == value.strip(" \t") and not _BASE64_SENTINEL_RE.fullmatch(value):
        return value
    encoded = base64.b64encode(value.encode("utf-8")).decode("ascii")
    return f"=?base64?{encoded}?="


def safe_auto_downgrade_version(error: BaseException, *, auto_mode: bool) -> str | None:
    """Return the highest supported legacy version only for explicit evidence."""

    if not auto_mode:
        return None
    supported: Sequence[str] | None = None
    if isinstance(error, MCPUnsupportedProtocolVersionError) and error.request_method == "server/discover":
        supported = error.supported_versions
    elif isinstance(error, MCPMethodNotFoundError) and error.request_method == "server/discover":
        supported = SUPPORTED_MCP_PROTOCOL_VERSION_ORDER[:-1]
    if supported is None:
        return None
    supported_set = set(supported)
    return next(
        (version for version in reversed(SUPPORTED_MCP_PROTOCOL_VERSION_ORDER[:-1]) if version in supported_set),
        None,
    )


def _normalize_tool(raw_tool: Any) -> Mapping[str, Any] | None:
    if not isinstance(raw_tool, Mapping):
        raise MCPProtocolError("tools/list tool entries must be objects.")
    name = _non_empty_string(raw_tool.get("name"), "Tool name")
    input_schema = raw_tool.get("inputSchema")
    if not isinstance(input_schema, Mapping):
        raise MCPProtocolError(f"Tool {name} inputSchema must be an object.")
    try:
        _validator_for(input_schema).check_schema(dict(input_schema))
    except SchemaError as exc:
        raise MCPProtocolError(f"Tool {name} inputSchema is not a valid JSON Schema.") from exc
    try:
        _header_annotations(input_schema)
    except MCPProtocolError:
        return None
    return dict(raw_tool)


def _validator_for(schema: Mapping[str, Any]):
    schema_uri = str(schema.get("$schema") or "").lower()
    return Draft7Validator if "draft-07" in schema_uri or "draft7" in schema_uri else Draft202012Validator


def _validate_arguments(schema: Mapping[str, Any], arguments: Mapping[str, Any]) -> None:
    try:
        _validator_for(schema)(dict(schema)).validate(dict(arguments))
    except ValidationError as exc:
        raise MCPProtocolError("Tool arguments failed inputSchema validation.") from exc


def _tool_parameter_headers(schema: Mapping[str, Any], arguments: Mapping[str, Any]) -> Mapping[str, str]:
    headers: dict[str, str] = {}
    for path, header_name, expected_type in _header_annotations(schema):
        value: Any = arguments
        for segment in path:
            if not isinstance(value, Mapping) or segment not in value:
                value = None
                break
            value = value[segment]
        if value is None:
            continue
        if expected_type == "boolean":
            encoded_value = "true" if value is True else "false"
        elif expected_type == "integer":
            if isinstance(value, bool) or not isinstance(value, int) or abs(value) > _MAX_SAFE_INTEGER:
                raise MCPProtocolError("MCP header integer value is outside the safe range.")
            encoded_value = str(value)
        elif expected_type == "string":
            encoded_value = str(value)
        else:
            raise MCPProtocolError("MCP header annotation has an unsupported primitive type.")
        headers[f"Mcp-Param-{header_name}"] = encode_mcp_header_value(encoded_value)
    return headers


def _header_annotations(schema: Mapping[str, Any]) -> tuple[tuple[tuple[str, ...], str, str], ...]:
    found: list[tuple[tuple[str, ...], str, str]] = []
    seen: set[str] = set()

    def walk(node: Any, path: tuple[str, ...], *, statically_reachable: bool) -> None:
        if not isinstance(node, Mapping):
            return
        if "x-mcp-header" in node:
            if not statically_reachable or not path:
                raise MCPProtocolError("x-mcp-header must be on a statically reachable property.")
            header_name = node.get("x-mcp-header")
            if not isinstance(header_name, str) or not _HEADER_TOKEN_RE.fullmatch(header_name):
                raise MCPProtocolError("x-mcp-header must be a non-empty HTTP token.")
            lowered = header_name.lower()
            if lowered in seen:
                raise MCPProtocolError("x-mcp-header must be case-insensitively unique.")
            expected_type = node.get("type")
            if expected_type not in {"string", "integer", "boolean"}:
                raise MCPProtocolError("x-mcp-header requires string, integer, or boolean type.")
            seen.add(lowered)
            found.append((path, header_name, str(expected_type)))
        properties = node.get("properties")
        if isinstance(properties, Mapping):
            for property_name, property_schema in properties.items():
                walk(property_schema, (*path, str(property_name)), statically_reachable=statically_reachable)
        for key, value in node.items():
            if key in {"properties", "x-mcp-header"}:
                continue
            if key in {"items", "oneOf", "anyOf", "allOf", "not", "if", "then", "else", "$ref"}:
                if _contains_header_annotation(value):
                    raise MCPProtocolError("x-mcp-header is not allowed behind dynamic schema keywords.")

    walk(schema, (), statically_reachable=True)
    return tuple(found)


def _contains_header_annotation(value: Any) -> bool:
    if isinstance(value, Mapping):
        return "x-mcp-header" in value or any(_contains_header_annotation(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_header_annotation(item) for item in value)
    return False


def _parse_cache_hint(result: Mapping[str, Any]) -> MCPListCacheHint:
    ttl_ms = _non_negative_int(result.get("ttlMs"), "Cache hint ttlMs")
    cache_scope = result.get("cacheScope")
    if cache_scope not in {"public", "private"}:
        raise MCPProtocolError("Cache hint cacheScope must be public or private.")
    return MCPListCacheHint(ttl_ms=ttl_ms, cache_scope=str(cache_scope))


def _validate_input_requests(
    value: Any,
    *,
    enable_elicitation: bool,
    require_nonempty: bool,
) -> Mapping[str, Mapping[str, Any]]:
    if value is None and not require_nonempty:
        return {}
    if not isinstance(value, Mapping) or (require_nonempty and not value):
        raise MCPProtocolError("InputRequiredResult inputRequests must be a non-empty object.")
    normalized: dict[str, Mapping[str, Any]] = {}
    for key, request in value.items():
        if not isinstance(key, str) or not key or not isinstance(request, Mapping):
            raise MCPProtocolError("InputRequiredResult inputRequests entries are invalid.")
        method = request.get("method")
        params = request.get("params")
        if method != "elicitation/create" or not enable_elicitation:
            raise MCPProtocolError("InputRequiredResult requested an undeclared client capability.")
        if not isinstance(params, Mapping):
            raise MCPProtocolError("InputRequiredResult request params must be an object.")
        normalized[key] = dict(request)
    return normalized


def _complete_ack(result: Mapping[str, Any], method: str) -> MCPCompletedOutcome:
    if result.get("resultType") != "complete":
        raise MCPProtocolError(f"{method} resultType must be complete.")
    return MCPCompletedOutcome(dict(result))


def _task_status(value: Any) -> str:
    if value not in _TASK_STATUSES:
        raise MCPProtocolError("Task status is invalid.")
    return str(value)


def _non_empty_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise MCPProtocolError(f"{field_name} must be a non-empty string.")
    return value


def _non_negative_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MCPProtocolError(f"{field_name} must be a non-negative integer.")
    return value


def _optional_non_negative_int(value: Any, field_name: str) -> int | None:
    if value is None:
        return None
    return _non_negative_int(value, field_name)


__all__ = [
    "MCP2026Adapter",
    "MCPCallOutcome",
    "MCPCompletedOutcome",
    "MCPDiscoverResult",
    "MCPInputRequiredOutcome",
    "MCPListCacheHint",
    "MCPMethodNotFoundError",
    "MCPTaskCreatedOutcome",
    "MCPTaskState",
    "MCPToolCatalogPage",
    "MCPUnsupportedProtocolVersionError",
    "encode_mcp_header_value",
    "safe_auto_downgrade_version",
]
