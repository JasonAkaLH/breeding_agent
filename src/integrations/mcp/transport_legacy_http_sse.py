from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from .client import MCPAuthRequiredError, MCPClientError, MCPProtocolError
from .config import MCPAuthConfig
from .protocol import (
    MCP_PROTOCOL_VERSION_2024_11_05,
    MCPStreamEvent,
    MCPTransportResponse,
    json_rpc_message_kind,
    normalize_json_rpc_response_id,
)
from .streaming_response import IncrementalJSONRPCResultParser, _MCPResultTarget
from .temporary_results import MCPResultSink
from .transport_http import (
    MCPPolicyBoundHTTPConnection,
    _response_from_http,
    _streaming_response_from_http,
    _validate_json_rpc_message,
)


@dataclass(slots=True)
class _PendingRequest:
    future: asyncio.Future[MCPTransportResponse]
    events: list[MCPStreamEvent] = field(default_factory=list)
    result_sink: MCPResultSink | None = None


@dataclass(slots=True, frozen=True)
class _PendingMatch:
    request_id: str | int
    pending: _PendingRequest
    message: Mapping[str, Any]


class LegacyHTTPSSETransport:
    """MCP 2024-11-05 HTTP+SSE transport.

    The legacy transport keeps the configured SSE endpoint open for the
    session, reads the server-provided `endpoint` event to discover the POST
    message endpoint, sends client JSON-RPC objects via POST, and correlates
    JSON-RPC responses delivered on the original SSE stream by request id. A
    direct POST body response remains supported for compatibility.
    """

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
            self._sse_endpoint = str(policy_bound_connection.endpoint_url)
            self._client = policy_bound_connection.client
            self._owns_client = True
        else:
            if not endpoint:
                raise ValueError("endpoint or policy_bound_connection is required.")
            self._sse_endpoint = endpoint
            self._client = client or httpx.AsyncClient()
            self._owns_client = client is None
        self._auth = auth or MCPAuthConfig()
        self._request_headers = {str(key): str(value) for key, value in dict(request_headers or {}).items()}
        self._post_endpoint: str | None = None
        self._post_endpoint_fingerprint: str = ""
        self._reader_task: asyncio.Task[None] | None = None
        self._endpoint_ready: asyncio.Future[str] | None = None
        self._pending: dict[str | int, _PendingRequest] = {}
        self._pending_lock = asyncio.Lock()
        self._closing = False
        self._unknown_response_count = 0

    @property
    def post_endpoint_fingerprint(self) -> str:
        return self._post_endpoint_fingerprint

    @property
    def post_endpoint(self) -> str | None:
        return self._post_endpoint

    @property
    def pending_request_count(self) -> int:
        return len(self._pending)

    @property
    def reader_task_done(self) -> bool:
        return self._reader_task is None or self._reader_task.done()

    @property
    def unknown_response_count(self) -> int:
        return self._unknown_response_count

    async def send(
        self,
        message: Mapping[str, Any],
        *,
        protocol_version: str,
        session_id: str | None = None,
        timeout_seconds: float | None = None,
        last_event_id: str | None = None,
    ) -> MCPTransportResponse:
        del session_id, last_event_id
        if protocol_version != MCP_PROTOCOL_VERSION_2024_11_05:
            raise MCPProtocolError("MCP legacy HTTP+SSE transport only supports protocol version 2024-11-05.")
        _validate_json_rpc_message(message)
        kind = json_rpc_message_kind(message)
        request_id = message.get("id") if kind == "request" else None
        pending: _PendingRequest | None = None
        post_endpoint = await self._ensure_post_endpoint(timeout_seconds=timeout_seconds)
        if kind == "request":
            if request_id is None:
                raise MCPProtocolError("MCP legacy HTTP+SSE request id must be a non-null string or integer.")
            pending = await self._register_pending(request_id)
        try:
            response = await self._post_message(post_endpoint, message, timeout_seconds=timeout_seconds)
            try:
                post_response = _response_from_http(response, session_id=None)
            except MCPAuthRequiredError:
                raise
            except MCPProtocolError:
                raise
            except MCPClientError as exc:
                if exc.mcp_error_code == "mcp_http_error":
                    raise MCPClientError(
                        "MCP legacy HTTP+SSE POST returned an error.",
                        code="legacy_post_failed",
                        retriable=exc.retriable,
                        metadata={"endpoint_fingerprint": self._post_endpoint_fingerprint, **exc.metadata},
                    ) from exc
                raise
            if kind != "request":
                return post_response
            if post_response.message is not None:
                await self._remove_pending(request_id)
                return post_response
            assert pending is not None
            try:
                return await asyncio.wait_for(pending.future, timeout=timeout_seconds)
            except TimeoutError as exc:
                await self._remove_pending(request_id)
                raise MCPClientError(
                    "MCP legacy HTTP+SSE response timed out.",
                    code="legacy_response_timeout",
                    retriable=True,
                    metadata={"endpoint_fingerprint": self._post_endpoint_fingerprint},
                ) from exc
        except Exception:
            if request_id is not None:
                await self._remove_pending(request_id)
            raise

    async def send_streaming(
        self,
        message: Mapping[str, Any],
        *,
        protocol_version: str,
        result_sink: MCPResultSink,
        session_id: str | None = None,
        timeout_seconds: float | None = None,
        last_event_id: str | None = None,
    ) -> MCPTransportResponse:
        """Stream a direct legacy POST response into the common result sink."""

        del session_id, last_event_id
        if protocol_version != MCP_PROTOCOL_VERSION_2024_11_05:
            raise MCPProtocolError("MCP legacy HTTP+SSE transport only supports protocol version 2024-11-05.")
        _validate_json_rpc_message(message)
        kind = json_rpc_message_kind(message)
        request_id = message.get("id") if kind == "request" else None
        pending: _PendingRequest | None = None
        post_endpoint = await self._ensure_post_endpoint(timeout_seconds=timeout_seconds)
        if kind == "request":
            if request_id is None:
                raise MCPProtocolError("MCP legacy HTTP+SSE request id must be a non-null string or integer.")
            pending = await self._register_pending(request_id, result_sink=result_sink)
        try:
            try:
                async with self._client.stream(
                    "POST",
                    post_endpoint,
                    json=dict(message),
                    headers={
                        "Accept": "application/json, text/event-stream",
                        "Content-Type": "application/json",
                        **self._request_headers,
                        **self._auth.headers(),
                    },
                    timeout=timeout_seconds,
                ) as response:
                    post_response = await _streaming_response_from_http(
                        response,
                        session_id=None,
                        result_sink=result_sink,
                        abort_sink_on_empty=False,
                    )
            except MCPClientError as exc:
                if exc.mcp_error_code == "mcp_http_error":
                    raise MCPClientError(
                        "MCP legacy HTTP+SSE POST returned an error.",
                        code="legacy_post_failed",
                        retriable=exc.retriable,
                        metadata={"endpoint_fingerprint": self._post_endpoint_fingerprint, **exc.metadata},
                    ) from exc
                raise
            if kind != "request":
                if post_response.message is None:
                    await result_sink.abort()
                return post_response
            if post_response.message is not None:
                await self._remove_pending(request_id)
                return post_response
            assert pending is not None
            try:
                return await asyncio.wait_for(pending.future, timeout=timeout_seconds)
            except TimeoutError as exc:
                await self._remove_pending(request_id)
                raise MCPClientError(
                    "MCP legacy HTTP+SSE response timed out.",
                    code="legacy_response_timeout",
                    retriable=True,
                    metadata={"endpoint_fingerprint": self._post_endpoint_fingerprint},
                ) from exc
        except httpx.TimeoutException as exc:
            await result_sink.abort()
            raise MCPClientError(
                "MCP legacy HTTP+SSE POST timed out.",
                code="legacy_post_failed",
                retriable=True,
                metadata={"endpoint_fingerprint": self._post_endpoint_fingerprint},
            ) from exc
        except httpx.HTTPError as exc:
            await result_sink.abort()
            raise MCPClientError(
                "MCP legacy HTTP+SSE POST failed.",
                code="legacy_post_failed",
                retriable=True,
                metadata={"endpoint_fingerprint": self._post_endpoint_fingerprint},
            ) from exc
        except BaseException:
            await result_sink.abort()
            if request_id is not None:
                await self._remove_pending(request_id)
            raise

    async def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        close_error = MCPClientError(
            "MCP legacy HTTP+SSE transport closed.",
            code="legacy_transport_closed",
            retriable=True,
            metadata={"endpoint_fingerprint": self._post_endpoint_fingerprint} if self._post_endpoint_fingerprint else {},
        )
        await self._fail_pending(close_error)
        if self._endpoint_ready is not None and not self._endpoint_ready.done():
            self._endpoint_ready.set_exception(close_error)
        if self._reader_task is not None and not self._reader_task.done():
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
        if self._owns_client:
            await self._client.aclose()

    async def _post_message(
        self,
        post_endpoint: str,
        message: Mapping[str, Any],
        *,
        timeout_seconds: float | None,
    ) -> httpx.Response:
        try:
            return await self._client.post(
                post_endpoint,
                json=dict(message),
                headers={
                    "Accept": "application/json, text/event-stream",
                    "Content-Type": "application/json",
                    **self._request_headers,
                    **self._auth.headers(),
                },
                timeout=timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise MCPClientError(
                "MCP legacy HTTP+SSE POST timed out.",
                code="legacy_post_failed",
                retriable=True,
                metadata={"endpoint_fingerprint": self._post_endpoint_fingerprint},
            ) from exc
        except httpx.HTTPError as exc:
            raise MCPClientError(
                "MCP legacy HTTP+SSE POST failed.",
                code="legacy_post_failed",
                retriable=True,
                metadata={"endpoint_fingerprint": self._post_endpoint_fingerprint},
            ) from exc

    async def _ensure_post_endpoint(self, *, timeout_seconds: float | None) -> str:
        if self._post_endpoint:
            return self._post_endpoint
        self._ensure_reader_started()
        assert self._endpoint_ready is not None
        try:
            return await asyncio.wait_for(asyncio.shield(self._endpoint_ready), timeout=timeout_seconds)
        except TimeoutError as exc:
            raise MCPClientError("MCP legacy HTTP+SSE endpoint event is missing.", code="legacy_endpoint_missing", retriable=False) from exc

    def _ensure_reader_started(self) -> None:
        if self._closing:
            raise MCPClientError("MCP legacy HTTP+SSE transport is closed.", code="legacy_transport_closed", retriable=True)
        if self._reader_task is not None and not self._reader_task.done():
            return
        loop = asyncio.get_running_loop()
        self._endpoint_ready = loop.create_future()
        self._reader_task = asyncio.create_task(self._reader_loop(), name="mcp-legacy-http-sse-reader")

    async def _reader_loop(self) -> None:
        parser = _LegacyStreamingSSEEventParser(
            sink_selector=self._result_target_for_response,
        )
        try:
            stream_context = self._client.stream(
                "GET",
                self._sse_endpoint,
                headers={"Accept": "text/event-stream", **self._request_headers, **self._auth.headers()},
            )
            async with stream_context as response:
                self._validate_sse_response(response)
                async for chunk in response.aiter_bytes():
                    for event in await parser.feed(chunk):
                        await self._handle_sse_event(event)
                for event in await parser.finish():
                    await self._handle_sse_event(event)
            if self._post_endpoint is None:
                self._set_endpoint_exception(
                    MCPClientError("MCP legacy HTTP+SSE endpoint event is missing.", code="legacy_endpoint_missing", retriable=False)
                )
            elif self._pending:
                await self._fail_pending(
                    MCPClientError(
                        "MCP legacy HTTP+SSE stream ended before response.",
                        code="legacy_sse_read_failed",
                        retriable=True,
                        metadata={"endpoint_fingerprint": self._post_endpoint_fingerprint},
                    )
                )
        except asyncio.CancelledError:
            raise
        except httpx.TimeoutException:
            wrapped = MCPClientError(
                "MCP legacy HTTP+SSE endpoint connect timed out.",
                code="legacy_sse_connect_failed",
                retriable=True,
            )
            self._set_endpoint_exception(wrapped)
            await self._fail_pending(wrapped)
        except httpx.HTTPError:
            wrapped = MCPClientError("MCP legacy HTTP+SSE endpoint connect failed.", code="legacy_sse_connect_failed", retriable=True)
            self._set_endpoint_exception(wrapped)
            await self._fail_pending(wrapped)
        except MCPClientError as exc:
            self._set_endpoint_exception(exc)
            await self._fail_pending(exc)
        except Exception:
            wrapped = MCPClientError(
                "MCP legacy HTTP+SSE stream read failed.",
                code="legacy_sse_read_failed",
                retriable=True,
                metadata={"endpoint_fingerprint": self._post_endpoint_fingerprint} if self._post_endpoint_fingerprint else {},
            )
            self._set_endpoint_exception(wrapped)
            await self._fail_pending(wrapped)
        finally:
            await parser.abort()

    def _validate_sse_response(self, response: httpx.Response) -> None:
        if response.status_code in {401, 403}:
            www_authenticate = response.headers.get("WWW-Authenticate", "")
            raise MCPAuthRequiredError(
                "MCP legacy HTTP+SSE server requires authorization or additional scope.",
                scope_required="insufficient_scope" in www_authenticate.lower(),
                metadata={"status_code": response.status_code, "www_authenticate_present": bool(www_authenticate)},
            )
        if response.status_code >= 400:
            raise MCPClientError(
                "MCP legacy HTTP+SSE endpoint returned an error.",
                code="legacy_sse_connect_failed",
                retriable=response.status_code >= 500,
                metadata={"status_code": response.status_code},
            )
        if "text/event-stream" not in response.headers.get("content-type", "").lower():
            raise MCPClientError(
                "MCP legacy HTTP+SSE endpoint did not return an SSE stream.",
                code="legacy_sse_connect_failed",
                retriable=False,
            )

    async def _handle_sse_event(self, event: MCPStreamEvent) -> None:
        if event.event == "endpoint" and event.data.strip():
            post_endpoint = self._validate_post_endpoint(event.data)
            self._post_endpoint = post_endpoint
            self._post_endpoint_fingerprint = _fingerprint(post_endpoint)
            if self._endpoint_ready is not None and not self._endpoint_ready.done():
                self._endpoint_ready.set_result(post_endpoint)
            return
        message = event.message
        if message is None:
            return
        try:
            kind = json_rpc_message_kind(message)
        except ValueError as exc:
            raise MCPProtocolError(str(exc)) from exc
        if kind == "response":
            match = self._match_pending_response(message)
            if match is None:
                self._unknown_response_count += 1
                return
            normalized_event = (
                event
                if match.message is message
                else replace(event, message=match.message)
            )
            events = (*match.pending.events, normalized_event)
            if not match.pending.future.done():
                match.pending.future.set_result(
                    MCPTransportResponse(
                        message=match.message,
                        headers={},
                        last_event_id=event.event_id,
                        sse_retry_ms=event.retry_ms,
                        sse_events=events,
                    )
                )
            await self._remove_pending(match.request_id)
            return
        for pending in self._pending.values():
            pending.events.append(event)

    async def _register_pending(
        self,
        request_id: str | int,
        *,
        result_sink: MCPResultSink | None = None,
    ) -> _PendingRequest:
        async with self._pending_lock:
            if request_id in self._pending:
                raise MCPProtocolError("MCP legacy HTTP+SSE duplicate pending request id.")
            pending = _PendingRequest(
                future=asyncio.get_running_loop().create_future(),
                result_sink=result_sink,
            )
            self._pending[request_id] = pending
            return pending

    async def _remove_pending(self, request_id: str | int) -> None:
        async with self._pending_lock:
            self._pending.pop(request_id, None)

    async def _fail_pending(self, exc: MCPClientError) -> None:
        async with self._pending_lock:
            pending_items = list(self._pending.values())
            self._pending.clear()
        for pending in pending_items:
            if pending.result_sink is not None:
                await pending.result_sink.abort()
            if not pending.future.done():
                pending.future.set_exception(exc)

    def _result_target_for_response(
        self,
        message: Mapping[str, Any],
    ) -> _MCPResultTarget | None:
        if "id" not in message:
            return None
        match = self._match_pending_response(message)
        if match is None:
            return None
        return _MCPResultTarget(
            response_id=match.message.get("id"),
            sink=match.pending.result_sink,
        )

    def _match_pending_response(
        self,
        message: Mapping[str, Any],
    ) -> _PendingMatch | None:
        alias: _PendingMatch | None = None
        for request_id, pending in self._pending.items():
            normalized = normalize_json_rpc_response_id(
                message,
                expected_request_id=request_id,
            )
            if normalized is None:
                continue
            match = _PendingMatch(request_id, pending, normalized)
            if normalized is message:
                return match
            if alias is None:
                alias = match
        return alias

    def _set_endpoint_exception(self, exc: MCPClientError) -> None:
        if self._endpoint_ready is not None and not self._endpoint_ready.done():
            self._endpoint_ready.set_exception(exc)

    def _validate_post_endpoint(self, endpoint_value: str) -> str:
        candidate = urljoin(self._sse_endpoint, endpoint_value.strip())
        parsed_candidate = urlparse(candidate)
        parsed_sse = urlparse(self._sse_endpoint)
        if parsed_candidate.scheme not in {"http", "https"} or not parsed_candidate.netloc:
            raise _invalid_endpoint(candidate)
        if parsed_candidate.scheme != parsed_sse.scheme:
            raise _invalid_endpoint(candidate)
        if _origin(parsed_candidate) != _origin(parsed_sse):
            raise _invalid_endpoint(candidate)
        return candidate


async def _read_endpoint_event(response: httpx.Response) -> tuple[str, str | None]:
    parser = _EndpointEventParser()
    async for line in response.aiter_lines():
        result = parser.feed(line)
        if result is not None:
            return result
    return parser.finish()


def _extract_endpoint_event(text: str) -> tuple[str, str | None]:
    return _extract_endpoint_event_from_lines(text.splitlines())


def _extract_endpoint_event_from_lines(lines: list[str]) -> tuple[str, str | None]:
    parser = _EndpointEventParser()
    for raw_line in lines:
        result = parser.feed(raw_line)
        if result is not None:
            return result
    return parser.finish()


class _LegacyStreamingSSEEventParser:
    """Byte-framed SSE parser that streams JSON message data to a result sink."""

    def __init__(self, *, sink_selector, max_control_bytes: int = 16 * 1024) -> None:
        self._sink_selector = sink_selector
        self._max_control_bytes = max_control_bytes
        self._field_name = bytearray()
        self._field_value = bytearray()
        self._field: str | None = None
        self._skip_space = False
        self._saw_cr = False
        self._event_name: str | None = None
        self._event_id: str | None = None
        self._retry_ms: int | None = None
        self._data = bytearray()
        self._data_line_seen = False
        self._json_parser: IncrementalJSONRPCResultParser | None = None
        self._pending_data_newline = False

    async def feed(self, chunk: bytes) -> list[MCPStreamEvent]:
        events: list[MCPStreamEvent] = []
        for byte in chunk:
            if self._saw_cr:
                self._saw_cr = False
                event = await self._end_line()
                if event is not None:
                    events.append(event)
                if byte == ord("\n"):
                    continue
            if byte == ord("\r"):
                self._saw_cr = True
            elif byte == ord("\n"):
                event = await self._end_line()
                if event is not None:
                    events.append(event)
            else:
                await self._feed_byte(byte)
        return events

    async def finish(self) -> list[MCPStreamEvent]:
        events: list[MCPStreamEvent] = []
        if self._saw_cr or self._field is not None or self._field_name:
            event = await self._end_line()
            if event is not None:
                events.append(event)
        if self._json_parser is not None or self._data or self._event_name or self._event_id or self._retry_ms is not None:
            event = await self._finish_event()
            if event is not None:
                events.append(event)
        return events

    async def abort(self) -> None:
        if self._json_parser is not None:
            await self._json_parser.abort()

    async def _feed_byte(self, byte: int) -> None:
        if self._field == "data":
            if self._skip_space:
                self._skip_space = False
                if byte == ord(" "):
                    return
            if self._is_message_event():
                if self._json_parser is None:
                    self._json_parser = IncrementalJSONRPCResultParser(
                        sink_selector=self._sink_selector,
                        require_response=False,
                    )
                if self._pending_data_newline:
                    await self._json_parser.feed(b"\n")
                    self._pending_data_newline = False
                await self._json_parser.feed(bytes((byte,)))
            else:
                self._data.append(byte)
                if len(self._data) > self._max_control_bytes:
                    raise MCPProtocolError("MCP legacy SSE control event exceeded metadata limit.")
            return
        if self._field is not None:
            if self._skip_space:
                self._skip_space = False
                if byte == ord(" "):
                    return
            self._field_value.append(byte)
            if len(self._field_value) > self._max_control_bytes:
                raise MCPProtocolError("MCP legacy SSE control line exceeded metadata limit.")
            return
        if byte == ord(":"):
            self._field = self._field_name.decode("ascii", errors="ignore")
            self._field_name.clear()
            self._skip_space = True
            return
        self._field_name.append(byte)
        if len(self._field_name) > self._max_control_bytes:
            raise MCPProtocolError("MCP legacy SSE field name exceeded metadata limit.")

    async def _end_line(self) -> MCPStreamEvent | None:
        if self._field is None and not self._field_name:
            event = await self._finish_event()
            self._reset_line()
            return event
        value = self._decode_value()
        if self._field == "event":
            self._event_name = value
        elif self._field == "id":
            self._event_id = value
        elif self._field == "retry":
            try:
                self._retry_ms = int(value)
            except ValueError:
                self._retry_ms = None
        elif self._field == "data" and self._json_parser is not None:
            self._pending_data_newline = True
        elif self._field == "data":
            self._data_line_seen = True
            self._data.extend(b"\n")
        self._reset_line()
        return None

    async def _finish_event(self) -> MCPStreamEvent | None:
        if self._json_parser is not None:
            parsed = await self._json_parser.finish()
            event = MCPStreamEvent(
                event=self._event_name or "message",
                event_id=self._event_id,
                retry_ms=self._retry_ms,
                data="",
                message=parsed.message,
                is_priming=False,
            )
        elif self._event_name is not None or self._event_id is not None or self._retry_ms is not None or self._data:
            try:
                data = self._data[:-1].decode("utf-8") if self._data_line_seen else self._data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise MCPProtocolError("MCP legacy SSE control event must be UTF-8.") from exc
            message = None
            if (self._event_name in {None, "message"}) and data:
                try:
                    message = json.loads(data)
                except json.JSONDecodeError as exc:
                    raise MCPProtocolError("MCP legacy HTTP+SSE message event data must be JSON-RPC JSON.") from exc
                _validate_json_rpc_message(message)
            event = MCPStreamEvent(
                event=self._event_name,
                event_id=self._event_id,
                retry_ms=self._retry_ms,
                data=data,
                message=message,
                is_priming=not bool(data),
            )
        else:
            return None
        self._event_name = None
        self._event_id = None
        self._retry_ms = None
        self._data.clear()
        self._data_line_seen = False
        self._json_parser = None
        self._pending_data_newline = False
        return event

    def _is_message_event(self) -> bool:
        return self._event_name in {None, "message"}

    def _decode_value(self) -> str:
        try:
            return self._field_value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MCPProtocolError("MCP legacy SSE control field must be UTF-8.") from exc

    def _reset_line(self) -> None:
        self._field_name.clear()
        self._field_value.clear()
        self._field = None
        self._skip_space = False


class _EndpointEventParser:
    def __init__(self) -> None:
        self._parser = _LegacySSEEventParser()

    def feed(self, raw_line: str) -> tuple[str, str | None] | None:
        event = self._parser.feed(raw_line)
        return _endpoint_tuple(event)

    def finish(self) -> tuple[str, str | None]:
        return _endpoint_tuple(self._parser.finish()) or ("", None)


class _LegacySSEEventParser:
    def __init__(self) -> None:
        self._event_name: str | None = None
        self._event_id: str | None = None
        self._retry_ms: int | None = None
        self._data_lines: list[str] = []

    def feed(self, raw_line: str) -> MCPStreamEvent | None:
        line = raw_line.rstrip("\r")
        if line == "":
            return self._flush()
        if line.startswith(":"):
            return None
        field_name, sep, value = line.partition(":")
        if not sep:
            return None
        value = value[1:] if value.startswith(" ") else value
        if field_name == "event":
            self._event_name = value
        elif field_name == "id":
            self._event_id = value
        elif field_name == "retry":
            try:
                self._retry_ms = int(value)
            except ValueError:
                self._retry_ms = None
        elif field_name == "data":
            self._data_lines.append(value)
        return None

    def finish(self) -> MCPStreamEvent | None:
        return self._flush()

    def _flush(self) -> MCPStreamEvent | None:
        if self._event_name is None and self._event_id is None and self._retry_ms is None and not self._data_lines:
            return None
        data = "\n".join(self._data_lines).strip()
        event_name = self._event_name or ("message" if data else None)
        event_id = self._event_id
        retry_ms = self._retry_ms
        self._event_name = None
        self._event_id = None
        self._retry_ms = None
        self._data_lines = []
        if not data:
            return MCPStreamEvent(event=event_name, event_id=event_id, retry_ms=retry_ms, data=data, message=None, is_priming=True)
        if event_name == "message":
            try:
                payload = json.loads(data)
            except json.JSONDecodeError as exc:
                raise MCPProtocolError("MCP legacy HTTP+SSE message event data must be JSON-RPC JSON.") from exc
            _validate_json_rpc_message(payload)
            return MCPStreamEvent(event=event_name, event_id=event_id, retry_ms=retry_ms, data=data, message=payload)
        return MCPStreamEvent(event=event_name, event_id=event_id, retry_ms=retry_ms, data=data, message=None)


def _endpoint_tuple(event: MCPStreamEvent | None) -> tuple[str, str | None] | None:
    if event is not None and event.event == "endpoint" and event.data:
        return event.data, event.event_id
    return None


def _origin(parsed) -> tuple[str, str, int | None]:
    return parsed.scheme, (parsed.hostname or "").lower(), _normalized_port(parsed)


def _normalized_port(parsed) -> int | None:
    if parsed.port is not None:
        return parsed.port
    if parsed.scheme == "https":
        return 443
    if parsed.scheme == "http":
        return 80
    return None


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _invalid_endpoint(candidate: str) -> MCPClientError:
    return MCPClientError(
        "MCP legacy HTTP+SSE endpoint event returned an invalid POST endpoint.",
        code="legacy_endpoint_invalid",
        retriable=False,
        metadata={"endpoint_fingerprint": _fingerprint(candidate)},
    )
