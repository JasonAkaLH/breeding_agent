from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from src.core.enums import UserMCPAuthType, UserMCPProtocolPreference, UserMCPTransport
from src.core.models import UserMCPServer

from .adapter import PythonLegacyMCPClientAdapter
from .adapter_2025_tasks import MCP2025TaskRecoveryClient, MCP2025TasksAdapter
from .adapter_2026 import MCP2026Adapter, safe_auto_downgrade_version
from .client import MCPClient, MCPProtocolError
from .credentials import CredentialCipher, CredentialSecurityError, MCPRecoveryService
from .endpoint_policy import EndpointPolicy, EndpointPolicyError, ValidatedEndpoint
from .policy_connection import build_policy_bound_http_connection
from .protocol import (
    MCP_PROTOCOL_VERSION_2025_11_25,
    MCP_PROTOCOL_VERSION_2026_07_28,
)
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
    def __init__(
        self,
        endpoint_policy: EndpointPolicy,
        *,
        recovery_service: MCPRecoveryService | None = None,
    ) -> None:
        self._endpoint_policy = endpoint_policy
        self._recovery_service = recovery_service

    async def revalidate_endpoint(self, server: UserMCPServer):
        return await asyncio.to_thread(
            self._endpoint_policy.validate, server.endpoint_url
        )

    async def create(self, server: UserMCPServer, request_headers: Mapping[str, Any]):
        endpoint = await asyncio.to_thread(
            self._endpoint_policy.validate, server.endpoint_url
        )
        return self._create_from_validated_endpoint(
            server,
            request_headers,
            endpoint,
        )

    async def create_readonly_shadow(
        self,
        server: UserMCPServer,
        request_headers: Mapping[str, Any],
        endpoint: ValidatedEndpoint,
    ):
        """Create a zero-call client from the gateway's exact policy decision."""

        if (
            not isinstance(endpoint, ValidatedEndpoint)
            or endpoint.normalized_url != server.endpoint_url
        ):
            raise EndpointPolicyError("mcp_endpoint_validation_binding_invalid")
        return self._create_from_validated_endpoint(
            server,
            request_headers,
            endpoint,
        )

    def _create_from_validated_endpoint(
        self,
        server: UserMCPServer,
        request_headers: Mapping[str, Any],
        endpoint: ValidatedEndpoint,
    ):
        headers = {str(key): str(value) for key, value in request_headers.items()}
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

    async def create_task_recovery(
        self,
        server: UserMCPServer,
        request_headers: Mapping[str, Any],
        *,
        protocol_version: str,
    ):
        """Create a pinned task query/control client from durable call-time state."""

        if protocol_version not in {
            MCP_PROTOCOL_VERSION_2025_11_25,
            MCP_PROTOCOL_VERSION_2026_07_28,
        }:
            raise MCPProtocolError(
                "mcp_remote_task_protocol_handler_unavailable"
            )
        if server.transport is not UserMCPTransport.STREAMABLE_HTTP:
            raise MCPProtocolError(
                "mcp_remote_task_protocol_handler_unavailable"
            )
        headers = {str(key): str(value) for key, value in request_headers.items()}
        endpoint = await asyncio.to_thread(
            self._endpoint_policy.validate, server.endpoint_url
        )
        preference = str(server.protocol_preference)
        if (
            preference != str(UserMCPProtocolPreference.AUTO)
            and preference != protocol_version
        ):
            raise MCPProtocolError(
                "mcp_remote_task_protocol_binding_mismatch"
            )
        if protocol_version == MCP_PROTOCOL_VERSION_2025_11_25:
            transport = StreamableHTTPTransport(
                policy_bound_connection=self._connection(endpoint),
                request_headers=headers,
            )
            if self._recovery_service is None:
                await transport.close()
                raise MCPProtocolError(
                    "mcp_remote_task_protocol_handler_unavailable"
                )
            return MCP2025TaskRecoveryClient(
                server_id=server.server_id,
                transport=transport,
                recovery_service=self._recovery_service,
                timeout_seconds=60,
            )
        return self._adapter_2026(
            server,
            headers,
            endpoint,
            recovery_only=True,
        )

    def _connection(self, endpoint):
        return build_policy_bound_http_connection(self._endpoint_policy, endpoint)

    def _adapter_2026(
        self,
        server: UserMCPServer,
        headers: Mapping[str, str],
        endpoint,
        *,
        recovery_only: bool = False,
    ):
        transport = StreamableHTTPTransport(
            policy_bound_connection=self._connection(endpoint), request_headers=headers
        )
        return MCP2026Adapter(
            server_id=server.server_id,
            transport=transport,
            timeout_seconds=60,
            enable_elicitation=True,
            enable_tasks=True,
            recovery_service=self._recovery_service,
            recovery_only=recovery_only,
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
        adapter = PythonLegacyMCPClientAdapter(
            MCPClient(
                server_id=server.server_id,
                transport=transport,
                protocol_version=version,
                timeout_seconds=60,
                pinned_protocol_version=True,
                transport_family=family,
            )
        )
        if version == MCP_PROTOCOL_VERSION_2025_11_25:
            return MCP2025TasksAdapter(
                adapter,
                server_id=server.server_id,
                recovery_service=self._recovery_service,
            )
        return adapter


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

    @property
    def supports_durable_recovery_context(self) -> bool:
        return bool(
            getattr(self._active, "supports_durable_recovery_context", False)
        )

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
