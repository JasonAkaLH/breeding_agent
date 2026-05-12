from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import httpx

from .client import MCPAuthRequiredError, MCPClientError, MCPProtocolError
from .config import MCPAuthConfig
from .protocol import MCPTransportResponse


class StreamableHTTPTransport:
    """MCP Streamable HTTP transport for JSON-RPC POST messages."""

    def __init__(
        self,
        *,
        endpoint: str,
        auth: MCPAuthConfig | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._endpoint = endpoint
        self._auth = auth or MCPAuthConfig()
        self._client = client or httpx.AsyncClient()
        self._owns_client = client is None

    async def send(
        self,
        message: Mapping[str, Any],
        *,
        protocol_version: str,
        session_id: str | None = None,
        timeout_seconds: float | None = None,
        last_event_id: str | None = None,
    ) -> MCPTransportResponse:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": protocol_version,
            **self._auth.headers(),
        }
        if session_id:
            headers["MCP-Session-Id"] = session_id
        if last_event_id:
            headers["Last-Event-ID"] = last_event_id
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

        if response.status_code in {401, 403}:
            www_authenticate = response.headers.get("WWW-Authenticate", "")
            raise MCPAuthRequiredError(
                "MCP server requires authorization or additional scope.",
                scope_required="insufficient_scope" in www_authenticate.lower(),
                metadata={"status_code": response.status_code, "www_authenticate_present": bool(www_authenticate)},
            )
        if response.status_code in {202, 204} or not response.content:
            return MCPTransportResponse(message=None, headers=_mcp_headers(response.headers))
        if response.status_code >= 400:
            raise MCPClientError("MCP HTTP server returned an error.", code="mcp_http_error", retriable=response.status_code >= 500, metadata={"status_code": response.status_code})

        content_type = response.headers.get("content-type", "").lower()
        if "text/event-stream" in content_type:
            message_payload, retry_ms, last_id = _parse_sse_response(response.text)
            return MCPTransportResponse(message=message_payload, headers=_mcp_headers(response.headers), sse_retry_ms=retry_ms, last_event_id=last_id)
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise MCPProtocolError("MCP HTTP response must be JSON or SSE JSON-RPC.") from exc
        if not isinstance(payload, Mapping):
            raise MCPProtocolError("MCP HTTP JSON response must be an object.")
        return MCPTransportResponse(message=payload, headers=_mcp_headers(response.headers))

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def _mcp_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {key: value for key, value in headers.items() if key.lower().startswith("mcp-")}


def _parse_sse_response(text: str) -> tuple[Mapping[str, Any], int | None, str | None]:
    retry_ms: int | None = None
    last_id: str | None = None
    data_lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r")
        if not line:
            if data_lines:
                break
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        value = value.lstrip(" ")
        if field == "data":
            data_lines.append(value)
        elif field == "id":
            last_id = value
        elif field == "retry":
            try:
                retry_ms = int(value)
            except ValueError:
                retry_ms = None
    if not data_lines:
        raise MCPProtocolError("MCP SSE response did not include data.")
    try:
        payload = json.loads("\n".join(data_lines))
    except json.JSONDecodeError as exc:
        raise MCPProtocolError("MCP SSE data must be JSON-RPC JSON.") from exc
    if not isinstance(payload, Mapping):
        raise MCPProtocolError("MCP SSE JSON payload must be an object.")
    return payload, retry_ms, last_id
