from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Protocol
from uuid import uuid4

from src.core.enums import (
    UserMCPAuthType,
    UserMCPHealthStatus,
    UserMCPProtocolPreference,
    UserMCPTransport,
)
from src.core.models import UserMCPCredentialRecord, UserMCPServer

from .credentials import CredentialCipher, EncryptedCredential
from .endpoint_policy import EndpointPolicy
from .headers import validate_auth_header_name, validate_static_headers
from .invalidation import (
    MCPInvalidationAction,
    MCPServerInvalidated,
)


class UserMCPConfigError(RuntimeError):
    def __init__(self, code: str, *, status_code: int = 422) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(code)


class MCPHealthScheduler(Protocol):
    async def start_test(self, server: UserMCPServer) -> Any: ...


class MCPInvalidationPublisher(Protocol):
    async def publish(self, event: MCPServerInvalidated) -> None: ...


class UserMCPConfigService:
    """Owner-scoped configuration boundary; credential values never leave this service."""

    def __init__(
        self,
        *,
        storage: Any,
        credential_cipher: CredentialCipher,
        endpoint_policy: EndpointPolicy,
        health_runner: MCPHealthScheduler | None = None,
        invalidation_bus: MCPInvalidationPublisher | None = None,
        now_fn: Any | None = None,
    ) -> None:
        self._storage = storage
        self._cipher = credential_cipher
        self._endpoint_policy = endpoint_policy
        self._health_runner = health_runner
        self._invalidation_bus = invalidation_bus
        self._now = now_fn or (lambda: datetime.now(timezone.utc).replace(tzinfo=None))
        self._deletion_task: Any | None = None
        self._closing = False

    async def list_servers(self, owner_user_id: str) -> list[UserMCPServer]:
        return await self._storage.list_user_mcp_servers(owner_user_id)

    async def get_server(self, owner_user_id: str, server_id: str) -> UserMCPServer:
        server = await self._storage.get_user_mcp_server(owner_user_id, server_id)
        if server is None:
            raise UserMCPConfigError("mcp_server_not_found", status_code=404)
        return server

    async def create_server(self, owner_user_id: str, payload: Mapping[str, Any]) -> UserMCPServer:
        now = self._now()
        server_id = f"mcp-{uuid4().hex}"
        endpoint = self._endpoint_policy.validate(str(payload["endpoint_url"]))
        transport, protocol = _validate_transport_protocol(
            payload.get("transport", UserMCPTransport.STREAMABLE_HTTP),
            payload.get("protocol_preference", UserMCPProtocolPreference.AUTO),
        )
        auth_type = _auth_type(payload.get("auth_type", UserMCPAuthType.NONE))
        auth_metadata, encrypted = self._prepare_new_credential(
            owner_user_id=owner_user_id,
            server_id=server_id,
            auth_type=auth_type,
            auth_metadata=payload.get("auth_metadata") or {},
            credential=payload.get("credential"),
        )
        enabled = bool(payload.get("enabled", True))
        server = UserMCPServer(
            server_id=server_id,
            owner_user_id=owner_user_id,
            display_name=str(payload["display_name"]),
            routing_description=str(payload.get("routing_description") or ""),
            endpoint_url=endpoint.normalized_url,
            transport=transport,
            protocol_preference=protocol,
            auth_type=auth_type,
            auth_metadata=auth_metadata,
            enabled=enabled,
            health_status=(
                UserMCPHealthStatus.UNTESTED if enabled else UserMCPHealthStatus.DISABLED
            ),
            credential_configured=encrypted is not None,
            created_at=now,
            updated_at=now,
        )
        credential_record = _credential_record(owner_user_id, server_id, encrypted, now)
        created = await self._storage.create_user_mcp_server(server, credential_record)
        if enabled:
            await self._start_health(created)
            return await self.get_server(owner_user_id, server_id)
        return created

    async def patch_server(
        self, owner_user_id: str, server_id: str, payload: Mapping[str, Any]
    ) -> UserMCPServer:
        current = await self.get_server(owner_user_id, server_id)
        values = {key: value for key, value in payload.items() if value is not None}
        changes: dict[str, Any] = {}
        for key in ("display_name", "routing_description", "enabled"):
            if key in values:
                changes[key] = values[key]

        endpoint_changed = "endpoint_url" in values
        if endpoint_changed:
            changes["endpoint_url"] = self._endpoint_policy.validate(
                str(values["endpoint_url"])
            ).normalized_url

        transport, protocol = _validate_transport_protocol(
            values.get("transport", current.transport),
            values.get("protocol_preference", current.protocol_preference),
        )
        if "transport" in values:
            changes["transport"] = transport
        if "protocol_preference" in values:
            changes["protocol_preference"] = protocol

        target_auth = _auth_type(values.get("auth_type", current.auth_type))
        credential_action = str(payload.get("credential_action") or "retain")
        raw_metadata = values.get("auth_metadata", current.auth_metadata)
        encrypted: EncryptedCredential | None = None
        if credential_action == "replace":
            auth_metadata, encrypted = self._prepare_new_credential(
                owner_user_id=owner_user_id,
                server_id=server_id,
                auth_type=target_auth,
                auth_metadata=raw_metadata,
                credential=payload.get("credential"),
            )
            changes["auth_metadata"] = auth_metadata
        elif credential_action == "clear":
            if target_auth is not UserMCPAuthType.NONE:
                raise UserMCPConfigError("mcp_credential_clear_requires_none_auth")
            changes["auth_metadata"] = {}
        elif credential_action == "retain":
            if target_auth != current.auth_type:
                raise UserMCPConfigError("mcp_auth_change_requires_credential_operation")
            if target_auth is UserMCPAuthType.NONE and current.credential_configured:
                raise UserMCPConfigError("mcp_credential_clear_required")
            retained_metadata = _validate_retained_auth_metadata(
                target_auth, raw_metadata, current.auth_metadata
            )
            if "auth_metadata" in values:
                changes["auth_metadata"] = retained_metadata
        else:
            raise UserMCPConfigError("mcp_credential_operation_invalid")
        if "auth_type" in values:
            changes["auth_type"] = target_auth

        security_sensitive = bool(
            endpoint_changed
            or "transport" in values
            or "protocol_preference" in values
            or "auth_type" in values
            or "auth_metadata" in values
            or credential_action in {"replace", "clear"}
        )
        target_enabled = bool(values.get("enabled", current.enabled))
        should_test = target_enabled and (
            security_sensitive
            or ("enabled" in values and not current.enabled)
            or current.health_status is UserMCPHealthStatus.TESTING
        )
        if not target_enabled:
            changes["health_status"] = UserMCPHealthStatus.DISABLED
            changes["last_test_error_code"] = None
        elif should_test:
            changes["health_status"] = UserMCPHealthStatus.UNTESTED
            changes["last_test_error_code"] = None

        updated_at = self._now()
        updated = await self._storage.update_user_mcp_server(
            owner_user_id,
            server_id,
            changes=changes,
            credential_operation=credential_action,
            credential=_credential_record(owner_user_id, server_id, encrypted, updated_at),
            security_sensitive=security_sensitive,
            updated_at=updated_at,
        )
        if updated is None:
            raise UserMCPConfigError("mcp_server_not_found", status_code=404)

        if not target_enabled:
            await self._publish(updated, MCPInvalidationAction.DISABLED)
        elif security_sensitive:
            await self._publish(updated, MCPInvalidationAction.SECURITY_UPDATED)
        if should_test:
            await self._start_health(updated)
            return await self.get_server(owner_user_id, server_id)
        return updated

    async def test_server(self, owner_user_id: str, server_id: str) -> UserMCPServer:
        current = await self.get_server(owner_user_id, server_id)
        if not current.enabled:
            raise UserMCPConfigError("mcp_server_disabled", status_code=409)
        now = self._now()
        updated = await self._storage.update_user_mcp_server(
            owner_user_id,
            server_id,
            changes={
                "health_status": UserMCPHealthStatus.UNTESTED,
                "last_test_error_code": None,
            },
            updated_at=now,
        )
        if updated is None:
            raise UserMCPConfigError("mcp_server_not_found", status_code=404)
        await self._start_health(updated)
        return await self.get_server(owner_user_id, server_id)

    async def delete_server(self, owner_user_id: str, server_id: str) -> bool:
        now = self._now()
        tombstone = await self._storage.mark_user_mcp_server_deleted(
            owner_user_id, server_id, deleted_at=now
        )
        if tombstone is None:
            raise UserMCPConfigError("mcp_server_not_found", status_code=404)
        await self._publish(tombstone, MCPInvalidationAction.DELETED)
        return bool(
            await self._storage.finalize_user_mcp_server_delete(
                owner_user_id, server_id, now=now
            )
        )

    async def reconcile_deletions_once(self) -> int:
        deleted = 0
        for server in await self._storage.list_pending_user_mcp_server_deletions():
            if await self._storage.finalize_user_mcp_server_delete(
                server.owner_user_id, server.server_id, now=self._now()
            ):
                deleted += 1
        return deleted

    async def start(self) -> None:
        if self._deletion_task is not None and not self._deletion_task.done():
            return
        self._closing = False
        await self.reconcile_deletions_once()
        self._deletion_task = asyncio.create_task(
            self._deletion_loop(), name="user-mcp-deletion-coordinator"
        )

    async def _deletion_loop(self) -> None:
        while not self._closing:
            await asyncio.sleep(10)
            try:
                await self.reconcile_deletions_once()
            except Exception:
                continue

    async def aclose(self) -> None:
        self._closing = True
        task = self._deletion_task
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._deletion_task = None

    def decrypt_credentials(self, server: UserMCPServer, record: Any) -> dict[str, Any]:
        return self._cipher.decrypt(
            record,
            owner_user_id=server.owner_user_id,
            server_id=server.server_id,
            auth_type=str(server.auth_type),
        )

    def _prepare_new_credential(
        self,
        *,
        owner_user_id: str,
        server_id: str,
        auth_type: UserMCPAuthType,
        auth_metadata: Mapping[str, Any],
        credential: Any,
    ) -> tuple[dict[str, Any], EncryptedCredential | None]:
        metadata = dict(auth_metadata)
        if auth_type is UserMCPAuthType.NONE:
            if metadata or credential is not None:
                raise UserMCPConfigError("mcp_none_auth_payload_invalid")
            return {}, None
        if credential is None:
            raise UserMCPConfigError("mcp_credential_required")
        secret, static_headers = _credential_values(credential)
        if auth_type is UserMCPAuthType.BEARER:
            if metadata or not secret or static_headers:
                raise UserMCPConfigError("mcp_bearer_credential_invalid")
            plaintext = {"token": secret}
            safe_metadata: dict[str, Any] = {}
        elif auth_type is UserMCPAuthType.API_KEY_HEADER:
            if set(metadata) != {"header_name"} or not secret or static_headers:
                raise UserMCPConfigError("mcp_api_key_credential_invalid")
            safe_metadata = {"header_name": validate_auth_header_name(metadata["header_name"])}
            plaintext = {"value": secret}
        else:
            if metadata or secret or not static_headers:
                raise UserMCPConfigError("mcp_static_header_credential_invalid")
            validated = validate_static_headers(static_headers)
            safe_metadata = {"header_names": list(validated.names)}
            plaintext = {"values": validated.credential_values.reveal()}
        encrypted = self._cipher.encrypt(
            owner_user_id=owner_user_id,
            server_id=server_id,
            auth_type=str(auth_type),
            values=plaintext,
        )
        return safe_metadata, encrypted

    async def _start_health(self, server: UserMCPServer) -> None:
        if self._health_runner is None:
            raise UserMCPConfigError("mcp_health_runner_unavailable", status_code=503)
        await self._health_runner.start_test(server)

    async def _publish(
        self, server: UserMCPServer, action: MCPInvalidationAction
    ) -> None:
        if self._invalidation_bus is None:
            return
        await self._invalidation_bus.publish(
            MCPServerInvalidated(
                owner_user_id=server.owner_user_id,
                server_id=server.server_id,
                security_version=server.security_version,
                action=action,
            )
        )


def _validate_transport_protocol(
    transport: Any, protocol: Any
) -> tuple[UserMCPTransport, UserMCPProtocolPreference]:
    try:
        normalized_transport = UserMCPTransport(str(transport))
        normalized_protocol = UserMCPProtocolPreference(str(protocol))
    except ValueError as exc:
        raise UserMCPConfigError("mcp_transport_protocol_invalid") from exc
    if normalized_transport is UserMCPTransport.LEGACY_HTTP_SSE:
        if normalized_protocol not in {
            UserMCPProtocolPreference.AUTO,
            UserMCPProtocolPreference.V2024_11_05,
        }:
            raise UserMCPConfigError("mcp_transport_protocol_invalid")
    elif normalized_protocol is UserMCPProtocolPreference.V2024_11_05:
        raise UserMCPConfigError("mcp_transport_protocol_invalid")
    return normalized_transport, normalized_protocol


def _auth_type(value: Any) -> UserMCPAuthType:
    try:
        return UserMCPAuthType(str(value))
    except ValueError as exc:
        raise UserMCPConfigError("mcp_auth_type_invalid") from exc


def _credential_values(credential: Any) -> tuple[str | None, dict[str, str]]:
    if hasattr(credential, "model_dump"):
        credential = credential.model_dump()
    if not isinstance(credential, Mapping):
        raise UserMCPConfigError("mcp_credential_payload_invalid")
    raw_secret = credential.get("secret_value")
    if hasattr(raw_secret, "get_secret_value"):
        raw_secret = raw_secret.get_secret_value()
    secret = str(raw_secret) if raw_secret is not None else None
    raw_headers = credential.get("static_headers") or {}
    headers: dict[str, str] = {}
    if not isinstance(raw_headers, Mapping):
        raise UserMCPConfigError("mcp_credential_payload_invalid")
    for name, raw_value in raw_headers.items():
        if hasattr(raw_value, "get_secret_value"):
            raw_value = raw_value.get_secret_value()
        headers[str(name)] = str(raw_value)
    return secret, headers


def _validate_retained_auth_metadata(
    auth_type: UserMCPAuthType,
    raw_metadata: Any,
    current_metadata: Mapping[str, Any],
) -> dict[str, Any]:
    metadata = dict(raw_metadata or {})
    if auth_type in {UserMCPAuthType.NONE, UserMCPAuthType.BEARER}:
        if metadata:
            raise UserMCPConfigError("mcp_auth_metadata_invalid")
        return {}
    if auth_type is UserMCPAuthType.API_KEY_HEADER:
        if set(metadata) != {"header_name"}:
            raise UserMCPConfigError("mcp_auth_metadata_invalid")
        return {"header_name": validate_auth_header_name(metadata["header_name"])}
    if metadata != dict(current_metadata):
        raise UserMCPConfigError("mcp_static_headers_require_replacement")
    return dict(current_metadata)


def _credential_record(
    owner_user_id: str,
    server_id: str,
    encrypted: EncryptedCredential | None,
    now: datetime,
) -> UserMCPCredentialRecord | None:
    if encrypted is None:
        return None
    return UserMCPCredentialRecord(
        owner_user_id=owner_user_id,
        server_id=server_id,
        credential_ciphertext=encrypted.credential_ciphertext,
        credential_nonce=encrypted.credential_nonce,
        encryption_version=encrypted.encryption_version,
        credential_updated_at=now,
    )
