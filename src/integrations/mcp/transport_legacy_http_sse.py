from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from .client import MCPAuthRequiredError, MCPClientError, MCPProtocolError
from .config import MCPAuthConfig
from .protocol import MCP_PROTOCOL_VERSION_2024_11_05, MCPStreamEvent, MCPTransportResponse, json_rpc_message_kind
from .transport_http import _response_from_http, _validate_json_rpc_message


@dataclass(slots=True)
class _PendingRequest:
    future: asyncio.Future[MCPTransportResponse]
    events: list[MCPStreamEvent] = field(default_factory=list)


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
        endpoint: str,
        auth: MCPAuthConfig | None = None,
        request_headers: Mapping[str, str] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._sse_endpoint = endpoint
        self._auth = auth or MCPAuthConfig()
        self._request_headers = {str(key): str(value) for key, value in dict(request_headers or {}).items()}
        self._client = client or httpx.AsyncClient()
        self._owns_client = client is None
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
        parser = _LegacySSEEventParser()
        try:
            stream_context = self._client.stream(
                "GET",
                self._sse_endpoint,
                headers={"Accept": "text/event-stream", **self._request_headers, **self._auth.headers()},
            )
            async with stream_context as response:
                self._validate_sse_response(response)
                async for line in response.aiter_lines():
                    event = parser.feed(line)
                    if event is not None:
                        await self._handle_sse_event(event)
                event = parser.finish()
                if event is not None:
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
            request_id = message.get("id")
            pending = self._pending.get(request_id)
            if pending is None:
                self._unknown_response_count += 1
                return
            events = (*pending.events, event)
            if not pending.future.done():
                pending.future.set_result(
                    MCPTransportResponse(
                        message=message,
                        headers={},
                        last_event_id=event.event_id,
                        sse_retry_ms=event.retry_ms,
                        sse_events=events,
                    )
                )
            await self._remove_pending(request_id)
            return
        for pending in self._pending.values():
            pending.events.append(event)

    async def _register_pending(self, request_id: str | int) -> _PendingRequest:
        async with self._pending_lock:
            if request_id in self._pending:
                raise MCPProtocolError("MCP legacy HTTP+SSE duplicate pending request id.")
            pending = _PendingRequest(future=asyncio.get_running_loop().create_future())
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
            if not pending.future.done():
                pending.future.set_exception(exc)

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
