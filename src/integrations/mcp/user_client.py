from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from src.core.enums import UserMCPAuthType, UserMCPProtocolPreference, UserMCPTransport
from src.core.models import UserMCPServer

from .adapter import PythonLegacyMCPClientAdapter
from .adapter_2026 import MCP2026Adapter, safe_auto_downgrade_version
from .client import MCPClient
from .credentials import CredentialCipher, CredentialSecurityError
from .endpoint_policy import EndpointPolicy
from .policy_connection import build_policy_bound_http_connection
from .protocol import MCP_PROTOCOL_VERSION_2026_07_28
from .transport_http import StreamableHTTPTransport
from .transport_legacy_http_sse import LegacyHTTPSSETransport


class UserMCPCredentialResolver:
    def __init__(self, storage: Any, cipher: CredentialCipher) -> None:
        self._storage = storage
        self._cipher = cipher

    async def request_headers_for(self, server: UserMCPServer) -> dict[str, str]:
        if server.auth_type is UserMCPAuthType.NONE:
            return {}
        record = await self._storage.get_user_mcp_credential(
            server.owner_user_id, server.server_id
        )
        if record is None:
            raise CredentialSecurityError("mcp_credential_missing")
        values = self._cipher.decrypt(
            record,
            owner_user_id=server.owner_user_id,
            server_id=server.server_id,
            auth_type=str(server.auth_type),
        )
        if server.auth_type is UserMCPAuthType.BEARER:
            return {"Authorization": f"Bearer {values['token']}"}
        if server.auth_type is UserMCPAuthType.API_KEY_HEADER:
            name = str(server.auth_metadata.get("header_name") or "")
            if not name:
                raise CredentialSecurityError("mcp_credential_metadata_invalid")
            return {name: str(values["value"])}
        headers = {str(key): str(value) for key, value in values["values"].items()}
        expected = {str(value).lower() for value in server.auth_metadata.get("header_names", ())}
        if set(headers) != expected:
            raise CredentialSecurityError("mcp_credential_metadata_invalid")
        return headers


class UserMCPClientFactory:
    def __init__(self, endpoint_policy: EndpointPolicy) -> None:
        self._endpoint_policy = endpoint_policy

    async def revalidate_endpoint(self, server: UserMCPServer):
        return await asyncio.to_thread(
            self._endpoint_policy.validate, server.endpoint_url
        )

    async def create(self, server: UserMCPServer, request_headers: Mapping[str, Any]):
        headers = {str(key): str(value) for key, value in request_headers.items()}
        endpoint = await asyncio.to_thread(
            self._endpoint_policy.validate, server.endpoint_url
        )
        if server.transport is UserMCPTransport.LEGACY_HTTP_SSE:
            return self._legacy_adapter(server, headers, endpoint, "2024-11-05")
        if server.protocol_preference is UserMCPProtocolPreference.AUTO:
            return _AutoNegotiatingAdapter(
                initial=self._adapter_2026(server, headers, endpoint),
                legacy_factory=lambda version: self._legacy_adapter(
                    server, headers, endpoint, version
                ),
            )
        version = str(server.protocol_preference)
        if version == MCP_PROTOCOL_VERSION_2026_07_28:
            return self._adapter_2026(server, headers, endpoint)
        return self._legacy_adapter(server, headers, endpoint, version)

    def _connection(self, endpoint):
        return build_policy_bound_http_connection(self._endpoint_policy, endpoint)

    def _adapter_2026(self, server: UserMCPServer, headers: Mapping[str, str], endpoint):
        transport = StreamableHTTPTransport(
            policy_bound_connection=self._connection(endpoint), request_headers=headers
        )
        return MCP2026Adapter(
            server_id=server.server_id,
            transport=transport,
            timeout_seconds=60,
            enable_elicitation=True,
            enable_tasks=True,
        )

    def _legacy_adapter(
        self, server: UserMCPServer, headers: Mapping[str, str], endpoint, version: str
    ):
        if server.transport is UserMCPTransport.LEGACY_HTTP_SSE:
            transport = LegacyHTTPSSETransport(
                policy_bound_connection=self._connection(endpoint), request_headers=headers
            )
            family = "legacy_http_sse"
        else:
            transport = StreamableHTTPTransport(
                policy_bound_connection=self._connection(endpoint), request_headers=headers
            )
            family = "streamable_http"
        return PythonLegacyMCPClientAdapter(
            MCPClient(
                server_id=server.server_id,
                transport=transport,
                protocol_version=version,
                timeout_seconds=60,
                pinned_protocol_version=True,
                transport_family=family,
            )
        )


class _AutoNegotiatingAdapter:
    def __init__(self, *, initial: Any, legacy_factory: Any) -> None:
        self._active = initial
        self._legacy_factory = legacy_factory

    @property
    def server_capabilities(self):
        return self._active.server_capabilities

    @property
    def negotiated_session(self):
        return self._active.negotiated_session

    async def initialize(self):
        try:
            return await self._active.initialize()
        except Exception as exc:
            version = safe_auto_downgrade_version(exc, auto_mode=True)
            if version is None:
                raise
            await self._active.close()
            self._active = self._legacy_factory(version)
            return await self._active.initialize()

    async def list_tools(self):
        return await self._active.list_tools()

    async def call_tool(self, *args, **kwargs):
        return await self._active.call_tool(*args, **kwargs)

    async def cancel_request(self, request_id, *, reason=""):
        cancel = getattr(self._active, "cancel_request", None)
        if cancel is None:
            raise NotImplementedError
        return await cancel(request_id, reason=reason)

    async def close(self):
        return await self._active.close()
