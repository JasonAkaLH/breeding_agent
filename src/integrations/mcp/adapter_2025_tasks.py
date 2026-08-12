from __future__ import annotations

import inspect
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .client import MCPProtocolError, MCPRemoteError
from .credentials import (
    CredentialSecurityError,
    MCPRecoveryCallContext,
    MCPRecoveryService,
)
from .protocol import (
    JSONRPC_VERSION,
    MCP_PROTOCOL_VERSION_2025_11_25,
    MCPNegotiatedSession,
    MCPTransport,
)

_RELATED_TASK_META_KEY = "io.modelcontextprotocol/related-task"
_TASK_STATUSES = frozenset(
    {"working", "input_required", "completed", "failed", "cancelled"}
)
_TERMINAL_TASK_STATUSES = frozenset({"completed", "failed", "cancelled"})
_RECOVERY_METHODS = frozenset({"tasks/get", "tasks/result", "tasks/cancel"})


@dataclass(slots=True, frozen=True)
class MCP2025TaskCreatedOutcome:
    safe_remote_task_ref: str
    status: str
    poll_interval_ms: int | None
    kind: str = "task_created"


@dataclass(slots=True, frozen=True)
class MCP2025TaskState:
    safe_remote_task_ref: str
    status: str
    terminal: bool
    poll_interval_ms: int | None = None
    status_message: str | None = None


@dataclass(slots=True, frozen=True)
class MCP2025TaskResult:
    safe_remote_task_ref: str
    call_tool_result: Mapping[str, Any]


@dataclass(slots=True, frozen=True)
class MCP2025TaskCancelAck:
    safe_remote_task_ref: str
    cancelled: bool


class MCP2025TasksAdapter:
    """Session-era execution adapter for the 2025 experimental Tasks wire."""

    protocol_version = MCP_PROTOCOL_VERSION_2025_11_25

    def __init__(
        self,
        client: Any,
        *,
        server_id: str,
        recovery_service: MCPRecoveryService | None,
        safe_ref_factory: Callable[[str], str] | None = None,
        task_ttl_ms: int = 60_000,
    ) -> None:
        self._client = client
        self.server_id = str(server_id)
        self._recovery_service = recovery_service
        self._safe_ref_factory = safe_ref_factory or (
            lambda prefix: f"{prefix}:{secrets.token_urlsafe(24)}"
        )
        self._task_ttl_ms = int(task_ttl_ms)
        self._task_support_by_tool: dict[str, str] = {}

    @property
    def server_capabilities(self) -> Mapping[str, Any]:
        value = getattr(self._client, "server_capabilities", {})
        return dict(value) if isinstance(value, Mapping) else {}

    @property
    def negotiated_session(self) -> MCPNegotiatedSession | None:
        value = getattr(self._client, "negotiated_session", None)
        return value if isinstance(value, MCPNegotiatedSession) else None

    @property
    def supports_durable_recovery_context(self) -> bool:
        return self._recovery_service is not None

    async def initialize(self) -> MCPNegotiatedSession:
        await self._client.initialize()
        session = self.negotiated_session
        if (
            session is None
            or session.negotiated_protocol_version
            != MCP_PROTOCOL_VERSION_2025_11_25
        ):
            raise MCPProtocolError(
                "MCP 2025 Tasks adapter requires a pinned 2025-11-25 session."
            )
        return session

    async def list_tools(self) -> list[Mapping[str, Any]]:
        raw_tools = await self._client.list_tools()
        tools = [dict(tool) for tool in raw_tools if isinstance(tool, Mapping)]
        support: dict[str, str] = {}
        server_supports_tasks = self._server_supports_task_augmented_tools_call()
        for tool in tools:
            name = str(tool.get("name") or "").strip()
            execution = tool.get("execution")
            task_support = (
                str(execution.get("taskSupport") or "forbidden").strip().lower()
                if isinstance(execution, Mapping)
                else "forbidden"
            )
            if task_support not in {"forbidden", "optional", "required"}:
                raise MCPProtocolError(
                    "MCP 2025 tool execution.taskSupport is invalid."
                )
            if task_support == "required" and not server_supports_tasks:
                raise MCPProtocolError(
                    "MCP 2025 task-augmented tools/call is required but was not negotiated."
                )
            if name:
                support[name] = (
                    task_support if server_supports_tasks else "forbidden"
                )
        self._task_support_by_tool = support
        return tools

    async def call_tool(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        recovery_context: MCPRecoveryCallContext | None = None,
        **kwargs: Any,
    ) -> Mapping[str, Any] | MCP2025TaskCreatedOutcome:
        call_kwargs = dict(kwargs)
        # These are 2026 MRTR-only Gateway fields. They must never cross onto
        # the 2025 experimental Tasks wire or the legacy MCPClient signature.
        call_kwargs.pop("input_responses", None)
        call_kwargs.pop("sealed_request_state_ref", None)
        task_required = self._task_support_by_tool.get(tool_name) == "required"
        if task_required:
            call_kwargs.setdefault("task_augmented", True)
            call_kwargs.setdefault(
                "progress_token",
                self._safe_ref_factory("mcp-progress"),
            )
            call_kwargs.setdefault("task_ttl_ms", self._task_ttl_ms)
            # CreateTaskResult is a small control object. Letting the streaming
            # path spool it would replace it with an opaque result reference
            # before the 2025 Task DTO can validate and durably bind taskId.
            call_kwargs.pop("result_sink", None)
        result = await self._client.call_tool(tool_name, arguments, **call_kwargs)
        if not isinstance(result, Mapping) or not _is_create_task_result(result):
            if task_required:
                raise MCPProtocolError(
                    "MCP 2025 required task-augmented tools/call did not return CreateTaskResult."
                )
            return dict(result) if isinstance(result, Mapping) else {"value": result}
        if not task_required:
            raise MCPProtocolError(
                "MCP 2025 server returned an unsolicited CreateTaskResult."
            )
        raw_task_id = _non_empty_string(result.get("taskId"), "CreateTaskResult taskId")
        status, _ = _task_status(result)
        poll_interval_ms = _optional_non_negative_int(
            result.get("pollInterval"), "CreateTaskResult pollInterval"
        )
        _validate_related_task_metadata(result, raw_task_id)
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
            raise MCPProtocolError("MCP 2025 remote task could not be bound.") from exc
        return MCP2025TaskCreatedOutcome(
            safe_remote_task_ref=safe_ref,
            status=status,
            poll_interval_ms=poll_interval_ms,
        )

    async def cancel_request(self, request_id: str | int, *, reason: str = "") -> Any:
        return await self._client.cancel_request(request_id, reason=reason)

    async def close(self) -> None:
        await _safe_close(self._client)

    def diagnostics(self) -> tuple[Any, ...]:
        diagnostics = getattr(self._client, "diagnostics", None)
        if not callable(diagnostics):
            return ()
        value = diagnostics()
        return tuple(value) if isinstance(value, tuple | list) else ()

    def _require_recovery_service(
        self, context: MCPRecoveryCallContext | None
    ) -> MCPRecoveryService:
        if self._recovery_service is None or context is None:
            raise MCPProtocolError(
                "MCP 2025 durable recovery context is required for remote Tasks."
            )
        return self._recovery_service

    def _server_supports_task_augmented_tools_call(self) -> bool:
        tasks = self.server_capabilities.get("tasks")
        requests = tasks.get("requests") if isinstance(tasks, Mapping) else None
        return isinstance(requests, Mapping) and isinstance(
            requests.get("tools.call"), Mapping
        )


class MCP2025TaskRecoveryClient:
    """Pinned query-only client for durable 2025 experimental Tasks recovery."""

    protocol_version = MCP_PROTOCOL_VERSION_2025_11_25

    def __init__(
        self,
        *,
        server_id: str,
        transport: MCPTransport,
        recovery_service: MCPRecoveryService,
        timeout_seconds: float | None = 60,
    ) -> None:
        self.server_id = str(server_id)
        self._transport = transport
        self._recovery_service = recovery_service
        self._timeout_seconds = timeout_seconds
        self._request_id = 0
        self._closed = False

    async def tasks_get(
        self,
        safe_remote_task_ref: str,
        *,
        recovery_context: MCPRecoveryCallContext,
    ) -> MCP2025TaskState:
        raw_task_id = await self._resolve_task_id(
            safe_remote_task_ref, recovery_context
        )
        result = await self._send_request("tasks/get", {"taskId": raw_task_id})
        response_task_id = _non_empty_string(result.get("taskId"), "Task taskId")
        if response_task_id != raw_task_id:
            raise MCPProtocolError(
                "MCP 2025 Task response taskId does not match the requested task."
            )
        status, message = _task_status(result)
        return MCP2025TaskState(
            safe_remote_task_ref=safe_remote_task_ref,
            status=status,
            terminal=status in _TERMINAL_TASK_STATUSES,
            poll_interval_ms=_optional_non_negative_int(
                result.get("pollInterval"), "Task pollInterval"
            ),
            status_message=message or None,
        )

    async def tasks_result(
        self,
        safe_remote_task_ref: str,
        *,
        recovery_context: MCPRecoveryCallContext,
    ) -> MCP2025TaskResult:
        raw_task_id = await self._resolve_task_id(
            safe_remote_task_ref, recovery_context
        )
        result = await self._send_request("tasks/result", {"taskId": raw_task_id})
        _validate_call_tool_result(result, raw_task_id)
        return MCP2025TaskResult(
            safe_remote_task_ref=safe_remote_task_ref,
            call_tool_result={
                str(key): value
                for key, value in result.items()
                if str(key) != "_meta"
            },
        )

    async def tasks_cancel(
        self,
        safe_remote_task_ref: str,
        *,
        recovery_context: MCPRecoveryCallContext,
        reason: str = "",
    ) -> MCP2025TaskCancelAck:
        raw_task_id = await self._resolve_task_id(
            safe_remote_task_ref, recovery_context
        )
        params: dict[str, Any] = {"taskId": raw_task_id}
        if reason:
            params["reason"] = reason
        result = await self._send_request("tasks/cancel", params)
        status, _ = _task_status(result, required=False)
        cancelled = result.get("cancelled") is True or status == "cancelled"
        return MCP2025TaskCancelAck(safe_remote_task_ref, cancelled)

    async def initialize(self) -> None:
        raise MCPProtocolError(
            "MCP 2025 recovery-only client does not permit initialize."
        )

    async def list_tools(self) -> None:
        raise MCPProtocolError(
            "MCP 2025 recovery-only client does not permit tools/list."
        )

    async def call_tool(self, *_args: Any, **_kwargs: Any) -> None:
        raise MCPProtocolError(
            "MCP 2025 recovery-only client does not permit tools/call."
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._transport.close()

    async def _resolve_task_id(
        self,
        safe_remote_task_ref: str,
        context: MCPRecoveryCallContext,
    ) -> str:
        try:
            return await self._recovery_service.resolve_remote_task_id(
                context,
                server_id=self.server_id,
                protocol_version=self.protocol_version,
                safe_remote_task_ref=safe_remote_task_ref,
            )
        except CredentialSecurityError as exc:
            raise MCPProtocolError(
                "Unknown or expired MCP 2025 remote task reference."
            ) from exc

    async def _send_request(
        self, method: str, params: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        if self._closed:
            raise MCPProtocolError("MCP 2025 recovery-only client is closed.")
        if method not in _RECOVERY_METHODS:
            raise MCPProtocolError(
                "MCP 2025 recovery-only client permits only Task recovery methods."
            )
        self._request_id += 1
        request_id = self._request_id
        response = await self._transport.send(
            {
                "jsonrpc": JSONRPC_VERSION,
                "id": request_id,
                "method": method,
                "params": dict(params),
            },
            protocol_version=self.protocol_version,
            session_id=None,
            timeout_seconds=self._timeout_seconds,
            last_event_id=None,
        )
        message = response.message
        if (
            not isinstance(message, Mapping)
            or message.get("jsonrpc") != JSONRPC_VERSION
            or message.get("id") != request_id
            or message.get("method") is not None
        ):
            raise MCPProtocolError("MCP 2025 Task response is invalid.")
        if "error" in message:
            error = message.get("error")
            if isinstance(error, Mapping):
                raise MCPRemoteError(
                    str(error.get("message") or "MCP server returned an error."),
                    remote_code=error.get("code"),
                )
            raise MCPRemoteError("MCP server returned an error.")
        result = message.get("result")
        if not isinstance(result, Mapping):
            raise MCPProtocolError("MCP 2025 Task result must be an object.")
        return dict(result)


def _is_create_task_result(result: Mapping[str, Any]) -> bool:
    return bool(str(result.get("taskId") or "").strip()) and isinstance(
        result.get("status"), Mapping
    )


def _task_status(
    payload: Mapping[str, Any], *, required: bool = True
) -> tuple[str, str]:
    raw_status = payload.get("status")
    status = raw_status if isinstance(raw_status, Mapping) else payload
    state = str(status.get("state") or "").strip().lower()
    if required and state not in _TASK_STATUSES:
        raise MCPProtocolError("MCP 2025 Task status is invalid.")
    if state and state not in _TASK_STATUSES:
        raise MCPProtocolError("MCP 2025 Task status is invalid.")
    message = str(status.get("message") or "").strip()
    return state, message


def _validate_related_task_metadata(
    payload: Mapping[str, Any], expected_task_id: str
) -> None:
    meta = payload.get("_meta")
    related = meta.get(_RELATED_TASK_META_KEY) if isinstance(meta, Mapping) else None
    if (
        not isinstance(related, Mapping)
        or str(related.get("taskId") or "") != expected_task_id
    ):
        raise MCPProtocolError(
            "MCP 2025 Task result related-task metadata is invalid."
        )


def _validate_call_tool_result(
    result: Mapping[str, Any], expected_task_id: str
) -> None:
    content = result.get("content")
    if not isinstance(content, list) or not all(
        isinstance(item, Mapping) for item in content
    ):
        raise MCPProtocolError("MCP 2025 tasks/result CallToolResult is invalid.")
    if "isError" in result and not isinstance(result.get("isError"), bool):
        raise MCPProtocolError("MCP 2025 tasks/result isError must be boolean.")
    if "structuredContent" in result and not isinstance(
        result.get("structuredContent"), Mapping
    ):
        raise MCPProtocolError(
            "MCP 2025 tasks/result structuredContent must be an object."
        )
    _validate_related_task_metadata(result, expected_task_id)


def _non_empty_string(value: Any, label: str) -> str:
    parsed = str(value or "").strip()
    if not parsed:
        raise MCPProtocolError(f"MCP 2025 {label} must be a non-empty string.")
    return parsed


def _optional_non_negative_int(value: Any, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MCPProtocolError(f"MCP 2025 {label} must be a non-negative integer.")
    return value


async def _safe_close(value: Any) -> None:
    close = getattr(value, "aclose", None) or getattr(value, "close", None)
    if not callable(close):
        return
    outcome = close()
    if inspect.isawaitable(outcome):
        await outcome


__all__ = [
    "MCP2025TaskCancelAck",
    "MCP2025TaskCreatedOutcome",
    "MCP2025TaskRecoveryClient",
    "MCP2025TaskResult",
    "MCP2025TaskState",
    "MCP2025TasksAdapter",
]
