from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

import httpx

from .client import MCPAuthRequiredError, MCPClientError, MCPProtocolError
from .config import MCPAuthConfig
from .protocol import MCP_PROTOCOL_VERSION_2026_07_28, MCPStreamEvent, MCPTransportResponse, json_rpc_message_kind
from .streaming_response import parse_json_rpc_byte_stream, parse_sse_json_rpc_byte_stream
from .temporary_results import MCPResultSink


@runtime_checkable
class MCPPolicyBoundHTTPConnection(Protocol):
    """Connector result produced by endpoint policy without coupling to its implementation."""

    @property
    def endpoint_url(self) -> str: ...

    @property
    def client(self) -> httpx.AsyncClient: ...


class StreamableHTTPTransport:
    """MCP Streamable HTTP transport for JSON-RPC POST and GET streams."""

    def __init__(
        self,
        *,
        endpoint: str | None = None,
        auth: MCPAuthConfig | None = None,
        request_headers: Mapping[str, str] | None = None,
        client: httpx.AsyncClient | None = None,
        policy_bound_connection: MCPPolicyBoundHTTPConnection | None = None,
    ) -> None:
        if policy_bound_connection is not None:
            if client is not None:
                raise ValueError("client and policy_bound_connection are mutually exclusive.")
            self._endpoint = str(policy_bound_connection.endpoint_url)
            self._client = policy_bound_connection.client
            self._owns_client = True
        else:
            if not endpoint:
                raise ValueError("endpoint or policy_bound_connection is required.")
            self._endpoint = endpoint
            self._client = client or httpx.AsyncClient()
            self._owns_client = client is None
        self._auth = auth or MCPAuthConfig()
        self._request_headers = {str(key): str(value) for key, value in dict(request_headers or {}).items()}

    async def send(
        self,
        message: Mapping[str, Any],
        *,
        protocol_version: str,
        session_id: str | None = None,
        timeout_seconds: float | None = None,
        last_event_id: str | None = None,
        request_headers: Mapping[str, str] | None = None,
    ) -> MCPTransportResponse:
        _validate_json_rpc_message(message)
        headers = self._post_headers(
            protocol_version=protocol_version,
            session_id=session_id,
            last_event_id=last_event_id,
            request_headers=request_headers,
        )
        try:
            response = await self._client.post(
                self._endpoint,
                json=dict(message),
                headers=headers,
                timeout=timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise MCPClientError("MCP HTTP request timed out.", code="mcp_timeout", retriable=True) from exc
        except httpx.HTTPError as exc:
            raise MCPClientError("MCP HTTP transport failed.", code="mcp_transport_error", retriable=True) from exc
        return _response_from_http(response, session_id=session_id, protocol_version=protocol_version)

    async def send_streaming(
        self,
        message: Mapping[str, Any],
        *,
        protocol_version: str,
        result_sink: MCPResultSink,
        session_id: str | None = None,
        timeout_seconds: float | None = None,
        last_event_id: str | None = None,
        request_headers: Mapping[str, str] | None = None,
    ) -> MCPTransportResponse:
        """Send a user-Gateway request without materializing its result body."""

        _validate_json_rpc_message(message)
        headers = self._post_headers(
            protocol_version=protocol_version,
            session_id=session_id,
            last_event_id=last_event_id,
            request_headers=request_headers,
        )
        try:
            async with self._client.stream(
                "POST",
                self._endpoint,
                json=dict(message),
                headers=headers,
                timeout=timeout_seconds,
            ) as response:
                return await _streaming_response_from_http(
                    response,
                    session_id=session_id,
                    result_sink=result_sink,
                    protocol_version=protocol_version,
                )
        except httpx.TimeoutException as exc:
            await result_sink.abort()
            raise MCPClientError("MCP HTTP request timed out.", code="mcp_timeout", retriable=True) from exc
        except httpx.HTTPError as exc:
            await result_sink.abort()
            raise MCPClientError("MCP HTTP transport failed.", code="mcp_transport_error", retriable=True) from exc
        except BaseException:
            await result_sink.abort()
            raise

    async def get_stream(
        self,
        *,
        protocol_version: str,
        session_id: str | None = None,
        timeout_seconds: float | None = None,
        last_event_id: str | None = None,
    ) -> MCPTransportResponse:
        headers = {
            "Accept": "text/event-stream",
            "MCP-Protocol-Version": protocol_version,
            **self._request_headers,
            **self._auth.headers(),
        }
        if session_id:
            headers["MCP-Session-Id"] = session_id
        if last_event_id:
            headers["Last-Event-ID"] = last_event_id
        try:
            response = await self._client.get(self._endpoint, headers=headers, timeout=timeout_seconds)
        except httpx.TimeoutException as exc:
            raise MCPClientError("MCP HTTP stream request timed out.", code="mcp_timeout", retriable=True) from exc
        except httpx.HTTPError as exc:
            raise MCPClientError("MCP HTTP stream failed.", code="mcp_transport_error", retriable=True) from exc
        if response.status_code == 405:
            return MCPTransportResponse(message=None, headers=_mcp_headers(response.headers))
        return _response_from_http(response, session_id=session_id, require_sse=True)

    async def delete_session(
        self,
        *,
        protocol_version: str,
        session_id: str,
        timeout_seconds: float | None = None,
    ) -> bool:
        headers = {
            "MCP-Protocol-Version": protocol_version,
            **self._request_headers,
            "MCP-Session-Id": session_id,
            **self._auth.headers(),
        }
        try:
            response = await self._client.delete(self._endpoint, headers=headers, timeout=timeout_seconds)
        except httpx.TimeoutException as exc:
            raise MCPClientError("MCP HTTP session delete timed out.", code="mcp_timeout", retriable=True) from exc
        except httpx.HTTPError as exc:
            raise MCPClientError("MCP HTTP session delete failed.", code="mcp_transport_error", retriable=True) from exc
        if response.status_code == 405:
            return False
        if response.status_code in {200, 202, 204}:
            return True
        if response.status_code == 404:
            raise MCPClientError("MCP HTTP session expired.", code="mcp_session_expired", retriable=True, metadata={"status_code": 404})
        if response.status_code >= 400:
            raise MCPClientError("MCP HTTP session delete returned an error.", code="mcp_http_error", retriable=response.status_code >= 500, metadata={"status_code": response.status_code})
        return True

    def _post_headers(
        self,
        *,
        protocol_version: str,
        session_id: str | None,
        last_event_id: str | None,
        request_headers: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": protocol_version,
            **self._request_headers,
            **{str(key): str(value) for key, value in dict(request_headers or {}).items()},
            **self._auth.headers(),
        }
        if session_id:
            headers["MCP-Session-Id"] = session_id
        if last_event_id:
            headers["Last-Event-ID"] = last_event_id
        return headers

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def _response_from_http(
    response: httpx.Response,
    *,
    session_id: str | None,
    require_sse: bool = False,
    protocol_version: str | None = None,
) -> MCPTransportResponse:
    if response.status_code in {401, 403}:
        www_authenticate = response.headers.get("WWW-Authenticate", "")
        raise MCPAuthRequiredError(
            "MCP server requires authorization or additional scope.",
            scope_required="insufficient_scope" in www_authenticate.lower(),
            metadata={"status_code": response.status_code, "www_authenticate_present": bool(www_authenticate)},
        )
    if response.status_code == 404 and session_id:
        raise MCPClientError("MCP HTTP session expired.", code="mcp_session_expired", retriable=True, metadata={"status_code": 404})
    if response.status_code in {202, 204} or not response.content:
        return MCPTransportResponse(message=None, headers=_mcp_headers(response.headers))
    if response.status_code in {400, 404} and protocol_version == MCP_PROTOCOL_VERSION_2026_07_28:
        try:
            payload = response.json()
            _validate_json_rpc_message(payload)
        except (json.JSONDecodeError, MCPProtocolError, ValueError, TypeError):
            pass
        else:
            error = payload.get("error") if isinstance(payload, Mapping) else None
            code = error.get("code") if isinstance(error, Mapping) else None
            if (response.status_code, code) in {(400, -32022), (404, -32601)}:
                return MCPTransportResponse(message=payload, headers=_mcp_headers(response.headers))
    if response.status_code >= 400:
        raise MCPClientError("MCP HTTP server returned an error.", code="mcp_http_error", retriable=response.status_code >= 500, metadata={"status_code": response.status_code})

    content_type = response.headers.get("content-type", "").lower()
    if "text/event-stream" in content_type:
        events = tuple(parse_sse_events(response.text))
        non_priming = [event for event in events if not event.is_priming]
        message = _select_response_message(non_priming)
        last_id = next((event.event_id for event in reversed(events) if event.event_id), None)
        retry_ms = next((event.retry_ms for event in reversed(events) if event.retry_ms is not None), None)
        return MCPTransportResponse(message=message, headers=_mcp_headers(response.headers), sse_retry_ms=retry_ms, last_event_id=last_id, sse_events=events)
    if require_sse:
        raise MCPProtocolError("MCP HTTP GET stream must return text/event-stream or 405.")
    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        raise MCPProtocolError("MCP HTTP response must be JSON or SSE JSON-RPC.") from exc
    _validate_json_rpc_message(payload)
    return MCPTransportResponse(message=payload, headers=_mcp_headers(response.headers))


async def _streaming_response_from_http(
    response: httpx.Response,
    *,
    session_id: str | None,
    result_sink: MCPResultSink,
    require_sse: bool = False,
    abort_sink_on_empty: bool = True,
    protocol_version: str | None = None,
) -> MCPTransportResponse:
    if response.status_code in {401, 403}:
        www_authenticate = response.headers.get("WWW-Authenticate", "")
        raise MCPAuthRequiredError(
            "MCP server requires authorization or additional scope.",
            scope_required="insufficient_scope" in www_authenticate.lower(),
            metadata={"status_code": response.status_code, "www_authenticate_present": bool(www_authenticate)},
        )
    if response.status_code == 404 and session_id:
        raise MCPClientError("MCP HTTP session expired.", code="mcp_session_expired", retriable=True, metadata={"status_code": 404})
    if response.status_code in {202, 204}:
        if abort_sink_on_empty:
            await result_sink.abort()
        return MCPTransportResponse(message=None, headers=_mcp_headers(response.headers))
    if response.status_code >= 400:
        await _consume_bounded_prefix(response)
        raise MCPClientError(
            "MCP HTTP server returned an error.",
            code="mcp_http_error",
            retriable=response.status_code >= 500,
            metadata={"status_code": response.status_code},
        )
    content_type = response.headers.get("content-type", "").lower()
    control_result_types = (
        frozenset({"input_required", "task"})
        if protocol_version == MCP_PROTOCOL_VERSION_2026_07_28
        else frozenset()
    )
    if "text/event-stream" in content_type:
        parsed = await parse_sse_json_rpc_byte_stream(
            response.aiter_bytes(),
            result_sink,
            control_result_types=control_result_types,
        )
        event = parsed.event
        events = parsed.events or ((event,) if event else ())
        return MCPTransportResponse(
            message=parsed.message,
            headers=_mcp_headers(response.headers),
            sse_retry_ms=event.retry_ms if event else None,
            last_event_id=event.event_id if event else None,
            sse_events=events,
        )
    if require_sse:
        raise MCPProtocolError("MCP HTTP stream must return text/event-stream.")
    parsed = await parse_json_rpc_byte_stream(
        response.aiter_bytes(),
        result_sink,
        control_result_types=control_result_types,
    )
    return MCPTransportResponse(message=parsed.message, headers=_mcp_headers(response.headers))


async def _consume_bounded_prefix(response: httpx.Response, *, limit: int = 4096) -> bytes:
    prefix = bytearray()
    async for chunk in response.aiter_bytes():
        remaining = limit - len(prefix)
        if remaining <= 0:
            break
        prefix.extend(chunk[:remaining])
        if len(prefix) >= limit:
            break
    return bytes(prefix)


def _select_response_message(events: list[MCPStreamEvent]) -> Mapping[str, Any] | None:
    for event in events:
        if event.message is None:
            continue
        try:
            if json_rpc_message_kind(event.message) == "response":
                return event.message
        except ValueError as exc:
            raise MCPProtocolError(str(exc)) from exc
    return events[-1].message if events else None


def _mcp_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {key: value for key, value in headers.items() if key.lower().startswith("mcp-")}


def parse_sse_events(text: str, *, max_event_bytes: int = 256 * 1024) -> list[MCPStreamEvent]:
    events: list[MCPStreamEvent] = []
    event_name: str | None = None
    event_id: str | None = None
    retry_ms: int | None = None
    data_lines: list[str] = []
    size = 0

    def flush() -> None:
        nonlocal event_name, event_id, retry_ms, data_lines, size
        if event_name is None and event_id is None and retry_ms is None and not data_lines:
            return
        data = "\n".join(data_lines)
        if data == "":
            events.append(MCPStreamEvent(event=event_name, event_id=event_id, retry_ms=retry_ms, data=data, message=None, is_priming=True))
        else:
            try:
                payload = json.loads(data)
            except json.JSONDecodeError as exc:
                raise MCPProtocolError("MCP SSE data must be JSON-RPC JSON.") from exc
            _validate_json_rpc_message(payload)
            events.append(MCPStreamEvent(event=event_name, event_id=event_id, retry_ms=retry_ms, data=data, message=payload, is_priming=False))
        event_name = None
        event_id = None
        retry_ms = None
        data_lines = []
        size = 0

    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r")
        size += len(line.encode("utf-8")) + 1
        if size > max_event_bytes:
            raise MCPProtocolError("MCP SSE event exceeded size limit.")
        if line == "":
            flush()
            continue
        if line.startswith(":"):
            continue
        field, sep, value = line.partition(":")
        if not sep:
            continue
        value = value[1:] if value.startswith(" ") else value
        if field == "data":
            data_lines.append(value)
        elif field == "id":
            event_id = value
        elif field == "event":
            event_name = value
        elif field == "retry":
            try:
                retry_ms = int(value)
            except ValueError:
                retry_ms = None
    flush()
    return events


def _validate_json_rpc_message(message: Any) -> Mapping[str, Any]:
    try:
        json_rpc_message_kind(message)
    except ValueError as exc:
        raise MCPProtocolError(str(exc)) from exc
    if not isinstance(message, Mapping):  # narrowed by json_rpc_message_kind
        raise MCPProtocolError("MCP JSON-RPC payload must be an object.")
    return message


__all__ = [
    "MCPPolicyBoundHTTPConnection",
    "StreamableHTTPTransport",
    "parse_sse_events",
]
