from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import stat
from datetime import datetime, timezone
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from src.core.models import (
    MCPCredentialKeyValidation,
    MCPRemoteTaskBinding,
    MCPSealedState,
)


MCP_CREDENTIAL_KEY_FILE_ENV = "MCP_CREDENTIAL_KEY_FILE"
CREDENTIAL_ENCRYPTION_VERSION = 1
MAX_CREDENTIAL_JSON_BYTES = 16 * 1024
MAX_TASK_PRIVATE_JSON_BYTES = 64 * 1024
MAX_REQUEST_STATE_BYTES = 32 * 1024
MAX_REMOTE_TASK_ID_BYTES = 4 * 1024
_NONCE_BYTES = 12
_AES_GCM_TAG_BYTES = 16
_SENTINEL_PLAINTEXT = b"mcp-credential-key-validation-v1"
_SENTINEL_AAD = b"mcp-credential-key-validation\x00v1"
_ALLOWED_AUTH_FIELDS = {
    "bearer": frozenset({"token"}),
    "api_key_header": frozenset({"value"}),
    "static_headers": frozenset({"values"}),
}
_TASK_PRIVATE_REQUEST_STATE_KIND = "request_state"
_TASK_PRIVATE_REMOTE_TASK_KIND = "remote_task_id"
_INITIAL_TERMINAL_REMOTE_TASK_STATUSES = frozenset(
    {"completed", "failed", "cancelled", "unknown"}
)


class CredentialSecurityError(RuntimeError):
    """A fail-closed credential error safe to expose in logs or health output."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class EncryptedCredential:
    nonce: bytes
    ciphertext: bytes
    encryption_version: int = CREDENTIAL_ENCRYPTION_VERSION

    @property
    def credential_nonce(self) -> bytes:
        return self.nonce

    @property
    def credential_ciphertext(self) -> bytes:
        return self.ciphertext

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(nonce=<redacted>, ciphertext=<redacted>, "
            f"encryption_version={self.encryption_version})"
        )


@dataclass(frozen=True, slots=True)
class MCPRecoveryCallContext:
    owner_user_id: str
    task_id: str
    node_id: str
    call_ref: str
    continuation_plan: Mapping[str, Any] | None = None


@runtime_checkable
class CredentialSentinelStorage(Protocol):
    """Minimal storage boundary needed for atomic sentinel create-or-verify."""

    async def get_mcp_credential_key_validation(self) -> MCPCredentialKeyValidation | None: ...

    async def create_or_get_mcp_credential_key_validation(
        self, record: MCPCredentialKeyValidation
    ) -> MCPCredentialKeyValidation: ...


@runtime_checkable
class MCPRecoveryStorage(Protocol):
    async def save_mcp_sealed_state(self, state: MCPSealedState) -> MCPSealedState: ...

    async def get_mcp_sealed_state(
        self, owner_user_id: str, task_id: str, sealed_state_ref: str
    ) -> MCPSealedState | None: ...

    async def save_mcp_remote_task_binding(
        self, binding: MCPRemoteTaskBinding
    ) -> MCPRemoteTaskBinding: ...

    async def get_mcp_remote_task_binding(
        self, owner_user_id: str, task_id: str, safe_remote_task_ref: str
    ) -> MCPRemoteTaskBinding | None: ...


def load_credential_key(
    path: str | os.PathLike[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    require_read_only: bool = False,
) -> bytes:
    environment = os.environ if environ is None else environ
    raw_path = os.fspath(path) if path is not None else environment.get(MCP_CREDENTIAL_KEY_FILE_ENV, "")
    if not raw_path:
        raise CredentialSecurityError("mcp_credential_key_file_missing")

    key_path = Path(raw_path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(key_path, flags)
    except OSError as exc:
        try:
            if key_path.is_symlink():
                raise CredentialSecurityError("mcp_credential_key_file_invalid_type") from exc
        except OSError:
            pass
        raise CredentialSecurityError("mcp_credential_key_file_unavailable") from exc
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise CredentialSecurityError("mcp_credential_key_file_invalid_type")
        allowed_permissions = {0o400} if require_read_only else {0o400, 0o600}
        if stat.S_IMODE(file_stat.st_mode) not in allowed_permissions:
            raise CredentialSecurityError("mcp_credential_key_file_invalid_permissions")
        with os.fdopen(descriptor, "rb") as key_file:
            descriptor = -1
            payload = key_file.read()
    except OSError as exc:
        raise CredentialSecurityError("mcp_credential_key_file_unavailable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if payload.endswith(b"\n"):
        payload = payload[:-1]
    if not payload or b"\n" in payload or b"\r" in payload or any(byte in b" \t\v\f" for byte in payload):
        raise CredentialSecurityError("mcp_credential_key_file_invalid_format")
    try:
        key = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise CredentialSecurityError("mcp_credential_key_file_invalid_format") from exc
    if base64.b64encode(key) != payload:
        raise CredentialSecurityError("mcp_credential_key_file_invalid_format")
    if len(key) != 32:
        raise CredentialSecurityError("mcp_credential_key_invalid_length")
    return key


class CredentialCipher:
    def __init__(self, key: bytes) -> None:
        if not isinstance(key, bytes) or len(key) != 32:
            raise CredentialSecurityError("mcp_credential_key_invalid_length")
        self._cipher = AESGCM(key)
        self._rollout_audit_key = hmac.new(
            key,
            b"mcp-rollout-audit-owner-v1",
            hashlib.sha256,
        ).digest()

    def safe_owner_reference(self, owner_user_id: str, *, context: str) -> str:
        owner = str(owner_user_id).strip()
        safe_context = str(context).strip()
        if not owner or not safe_context:
            raise CredentialSecurityError("mcp_rollout_audit_identity_invalid")
        return hmac.new(
            self._rollout_audit_key,
            f"{safe_context}\0{owner}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    @classmethod
    def from_key_file(
        cls,
        path: str | os.PathLike[str] | None = None,
        *,
        environ: Mapping[str, str] | None = None,
        require_read_only: bool = False,
    ) -> "CredentialCipher":
        return cls(
            load_credential_key(
                path,
                environ=environ,
                require_read_only=require_read_only,
            )
        )

    def encrypt(
        self,
        *,
        owner_user_id: str,
        server_id: str,
        auth_type: str,
        values: Mapping[str, Any],
        encryption_version: int = CREDENTIAL_ENCRYPTION_VERSION,
    ) -> EncryptedCredential:
        plaintext = _encode_credential(auth_type, values)
        nonce = os.urandom(_NONCE_BYTES)
        ciphertext = self._cipher.encrypt(
            nonce,
            plaintext,
            _credential_aad(owner_user_id, server_id, encryption_version),
        )
        return EncryptedCredential(nonce=nonce, ciphertext=ciphertext, encryption_version=encryption_version)

    def decrypt(
        self,
        record: EncryptedCredential | Mapping[str, Any] | Any,
        *,
        owner_user_id: str,
        server_id: str,
        auth_type: str,
    ) -> dict[str, Any]:
        encrypted = _coerce_encrypted_record(record)
        if encrypted.encryption_version != CREDENTIAL_ENCRYPTION_VERSION:
            raise CredentialSecurityError("mcp_credential_decryption_failed")
        try:
            plaintext = self._cipher.decrypt(
                encrypted.nonce,
                encrypted.ciphertext,
                _credential_aad(owner_user_id, server_id, encrypted.encryption_version),
            )
            if len(plaintext) > MAX_CREDENTIAL_JSON_BYTES:
                raise CredentialSecurityError("mcp_credential_payload_too_large")
            decoded = json.loads(plaintext.decode("utf-8"))
            return _validate_credential(auth_type, decoded)
        except (CredentialSecurityError, InvalidTag, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise CredentialSecurityError("mcp_credential_decryption_failed") from exc

    def seal_task_private_payload(
        self,
        *,
        owner_user_id: str,
        task_id: str,
        node_id: str,
        call_ref: str,
        state_kind: str,
        server_id: str,
        protocol_version: str,
        payload: Mapping[str, Any],
        encryption_version: int = CREDENTIAL_ENCRYPTION_VERSION,
    ) -> EncryptedCredential:
        try:
            plaintext = json.dumps(
                dict(payload),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise CredentialSecurityError("mcp_task_private_payload_invalid") from exc
        if len(plaintext) > MAX_TASK_PRIVATE_JSON_BYTES:
            raise CredentialSecurityError("mcp_task_private_payload_too_large")
        nonce = os.urandom(_NONCE_BYTES)
        ciphertext = self._cipher.encrypt(
            nonce,
            plaintext,
            _task_private_aad(
                owner_user_id=owner_user_id,
                task_id=task_id,
                node_id=node_id,
                call_ref=call_ref,
                state_kind=state_kind,
                server_id=server_id,
                protocol_version=protocol_version,
                encryption_version=encryption_version,
            ),
        )
        return EncryptedCredential(
            nonce=nonce,
            ciphertext=ciphertext,
            encryption_version=encryption_version,
        )

    def unseal_task_private_payload(
        self,
        record: EncryptedCredential | Mapping[str, Any] | Any,
        *,
        owner_user_id: str,
        task_id: str,
        node_id: str,
        call_ref: str,
        state_kind: str,
        server_id: str,
        protocol_version: str,
    ) -> dict[str, Any]:
        encrypted = _coerce_encrypted_record(record)
        if encrypted.encryption_version != CREDENTIAL_ENCRYPTION_VERSION:
            raise CredentialSecurityError("mcp_task_private_decryption_failed")
        if len(encrypted.ciphertext) > MAX_TASK_PRIVATE_JSON_BYTES + _AES_GCM_TAG_BYTES:
            raise CredentialSecurityError("mcp_task_private_decryption_failed")
        try:
            plaintext = self._cipher.decrypt(
                encrypted.nonce,
                encrypted.ciphertext,
                _task_private_aad(
                    owner_user_id=owner_user_id,
                    task_id=task_id,
                    node_id=node_id,
                    call_ref=call_ref,
                    state_kind=state_kind,
                    server_id=server_id,
                    protocol_version=protocol_version,
                    encryption_version=encrypted.encryption_version,
                ),
            )
            if len(plaintext) > MAX_TASK_PRIVATE_JSON_BYTES:
                raise CredentialSecurityError("mcp_task_private_decryption_failed")
            decoded = json.loads(plaintext.decode("utf-8"))
            if not isinstance(decoded, Mapping):
                raise ValueError
            return dict(decoded)
        except (
            CredentialSecurityError,
            InvalidTag,
            UnicodeDecodeError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ) as exc:
            raise CredentialSecurityError("mcp_task_private_decryption_failed") from exc

    async def create_or_verify_sentinel(self, storage: CredentialSentinelStorage) -> None:
        try:
            current = await storage.get_mcp_credential_key_validation()
        except Exception as exc:
            raise CredentialSecurityError("mcp_credential_sentinel_unavailable") from exc
        if current is not None:
            self._verify_sentinel(current)
            return

        nonce = os.urandom(_NONCE_BYTES)
        candidate = MCPCredentialKeyValidation(
            validation_id=str(uuid4()),
            validation_nonce=nonce,
            validation_ciphertext=self._cipher.encrypt(nonce, _SENTINEL_PLAINTEXT, _SENTINEL_AAD),
            encryption_version=CREDENTIAL_ENCRYPTION_VERSION,
            created_at=datetime.now(timezone.utc),
        )
        try:
            winner = await storage.create_or_get_mcp_credential_key_validation(candidate)
        except Exception as exc:
            raise CredentialSecurityError("mcp_credential_sentinel_unavailable") from exc
        self._verify_sentinel(winner)

    def _verify_sentinel(self, record: MCPCredentialKeyValidation | Mapping[str, Any] | Any) -> None:
        try:
            if isinstance(record, Mapping):
                encrypted = EncryptedCredential(
                    nonce=record["validation_nonce"],
                    ciphertext=record["validation_ciphertext"],
                    encryption_version=int(record["encryption_version"]),
                )
            else:
                encrypted = EncryptedCredential(
                    nonce=record.validation_nonce,
                    ciphertext=record.validation_ciphertext,
                    encryption_version=int(record.encryption_version),
                )
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise CredentialSecurityError("mcp_credential_sentinel_mismatch") from exc
        if encrypted.encryption_version != CREDENTIAL_ENCRYPTION_VERSION:
            raise CredentialSecurityError("mcp_credential_sentinel_mismatch")
        try:
            plaintext = self._cipher.decrypt(encrypted.nonce, encrypted.ciphertext, _SENTINEL_AAD)
        except (InvalidTag, TypeError, ValueError) as exc:
            raise CredentialSecurityError("mcp_credential_sentinel_mismatch") from exc
        if plaintext != _SENTINEL_PLAINTEXT:
            raise CredentialSecurityError("mcp_credential_sentinel_mismatch")


class MCPRecoveryService:
    """Durable, task-private storage for opaque MCP continuation values."""

    def __init__(
        self,
        storage: MCPRecoveryStorage,
        cipher: CredentialCipher,
        *,
        now_fn: Any | None = None,
    ) -> None:
        self._storage = storage
        self._cipher = cipher
        self._now = now_fn or (lambda: datetime.now(timezone.utc).replace(tzinfo=None))

    async def save_request_state(
        self,
        context: MCPRecoveryCallContext,
        *,
        server_id: str,
        protocol_version: str,
        sealed_state_ref: str,
        request_state: str,
        tool_name: str,
        arguments: Mapping[str, Any],
    ) -> None:
        _validate_private_string_size(
            request_state,
            maximum=MAX_REQUEST_STATE_BYTES,
            error_code="mcp_request_state_too_large",
        )
        encrypted = self._cipher.seal_task_private_payload(
            owner_user_id=context.owner_user_id,
            task_id=context.task_id,
            node_id=context.node_id,
            call_ref=context.call_ref,
            state_kind=_TASK_PRIVATE_REQUEST_STATE_KIND,
            server_id=server_id,
            protocol_version=protocol_version,
            payload={
                "request_state": request_state,
                "tool_name": tool_name,
                "arguments": dict(arguments),
            },
        )
        now = self._now()
        candidate = MCPSealedState(
            sealed_state_ref=sealed_state_ref,
            owner_user_id=context.owner_user_id,
            task_id=context.task_id,
            node_id=context.node_id,
            call_ref=context.call_ref,
            state_kind=_TASK_PRIVATE_REQUEST_STATE_KIND,
            ciphertext=encrypted.ciphertext,
            nonce=encrypted.nonce,
            encryption_version=encrypted.encryption_version,
            created_at=now,
            updated_at=now,
        )
        try:
            saved = await self._storage.save_mcp_sealed_state(candidate)
            payload = self._unseal_request_state(
                saved,
                expected_context=context,
                server_id=server_id,
                protocol_version=protocol_version,
            )
        except CredentialSecurityError:
            raise
        except Exception as exc:
            raise CredentialSecurityError("mcp_recovery_persistence_failed") from exc
        if payload != {
            "request_state": request_state,
            "tool_name": tool_name,
            "arguments": dict(arguments),
        }:
            raise CredentialSecurityError("mcp_recovery_persistence_failed")

    async def resolve_request_state(
        self,
        expected_context: MCPRecoveryCallContext,
        *,
        server_id: str,
        protocol_version: str,
        sealed_state_ref: str,
        tool_name: str,
        arguments: Mapping[str, Any],
    ) -> str:
        try:
            record = await self._storage.get_mcp_sealed_state(
                expected_context.owner_user_id,
                expected_context.task_id,
                sealed_state_ref,
            )
        except Exception as exc:
            raise CredentialSecurityError("mcp_recovery_unavailable") from exc
        if (
            record is None
            or record.state_kind != _TASK_PRIVATE_REQUEST_STATE_KIND
            or not _record_matches_context(record, expected_context)
        ):
            raise CredentialSecurityError("mcp_recovery_reference_not_found")
        payload = self._unseal_request_state(
            record,
            expected_context=expected_context,
            server_id=server_id,
            protocol_version=protocol_version,
        )
        if payload.get("tool_name") != tool_name or payload.get("arguments") != dict(arguments):
            raise CredentialSecurityError("mcp_recovery_request_mismatch")
        request_state = payload.get("request_state")
        if not isinstance(request_state, str):
            raise CredentialSecurityError("mcp_task_private_decryption_failed")
        _validate_private_string_size(
            request_state,
            maximum=MAX_REQUEST_STATE_BYTES,
            error_code="mcp_task_private_decryption_failed",
        )
        return request_state

    async def save_remote_task(
        self,
        context: MCPRecoveryCallContext,
        *,
        server_id: str,
        protocol_version: str,
        safe_remote_task_ref: str,
        remote_task_id: str,
        status: str,
        poll_interval_ms: int | None,
    ) -> None:
        _validate_private_string_size(
            remote_task_id,
            maximum=MAX_REMOTE_TASK_ID_BYTES,
            error_code="mcp_remote_task_id_too_large",
        )
        encrypted = self._cipher.seal_task_private_payload(
            owner_user_id=context.owner_user_id,
            task_id=context.task_id,
            node_id=context.node_id,
            call_ref=context.call_ref,
            state_kind=_TASK_PRIVATE_REMOTE_TASK_KIND,
            server_id=server_id,
            protocol_version=protocol_version,
            payload={"remote_task_id": remote_task_id},
        )
        now = self._now()
        # Publication is a separate aggregate barrier after Branch and TaskNode
        # have durably entered their waiting states. Until then the recovery
        # worker must not observe this binding as due.
        next_poll_at = None
        candidate = MCPRemoteTaskBinding(
            safe_remote_task_ref=safe_remote_task_ref,
            owner_user_id=context.owner_user_id,
            task_id=context.task_id,
            node_id=context.node_id,
            call_ref=context.call_ref,
            server_id=server_id,
            protocol_version=protocol_version,
            remote_task_ciphertext=encrypted.ciphertext,
            remote_task_nonce=encrypted.nonce,
            encryption_version=encrypted.encryption_version,
            last_status=status,
            next_poll_at=next_poll_at,
            continuation_plan=dict(context.continuation_plan or {}),
            created_at=now,
            updated_at=now,
            terminal_at=None,
        )
        try:
            saved = await self._storage.save_mcp_remote_task_binding(candidate)
            saved_remote_task_id = self._unseal_remote_task_id(
                saved,
                expected_context=context,
            )
        except CredentialSecurityError:
            raise
        except Exception as exc:
            raise CredentialSecurityError("mcp_recovery_persistence_failed") from exc
        if saved_remote_task_id != remote_task_id:
            raise CredentialSecurityError("mcp_recovery_persistence_failed")

    async def resolve_remote_task_id(
        self,
        expected_context: MCPRecoveryCallContext,
        *,
        server_id: str,
        protocol_version: str,
        safe_remote_task_ref: str,
    ) -> str:
        try:
            record = await self._storage.get_mcp_remote_task_binding(
                expected_context.owner_user_id,
                expected_context.task_id,
                safe_remote_task_ref,
            )
        except Exception as exc:
            raise CredentialSecurityError("mcp_recovery_unavailable") from exc
        if (
            record is None
            or record.server_id != server_id
            or record.protocol_version != protocol_version
            or not _record_matches_context(record, expected_context)
        ):
            raise CredentialSecurityError("mcp_recovery_reference_not_found")
        return self._unseal_remote_task_id(record, expected_context=expected_context)

    def _unseal_request_state(
        self,
        record: MCPSealedState,
        *,
        expected_context: MCPRecoveryCallContext,
        server_id: str,
        protocol_version: str,
    ) -> dict[str, Any]:
        return self._cipher.unseal_task_private_payload(
            EncryptedCredential(
                nonce=record.nonce,
                ciphertext=record.ciphertext,
                encryption_version=record.encryption_version,
            ),
            owner_user_id=expected_context.owner_user_id,
            task_id=expected_context.task_id,
            node_id=expected_context.node_id,
            call_ref=expected_context.call_ref,
            state_kind=record.state_kind,
            server_id=server_id,
            protocol_version=protocol_version,
        )

    def _unseal_remote_task_id(
        self,
        record: MCPRemoteTaskBinding,
        *,
        expected_context: MCPRecoveryCallContext,
    ) -> str:
        payload = self._cipher.unseal_task_private_payload(
            EncryptedCredential(
                nonce=record.remote_task_nonce,
                ciphertext=record.remote_task_ciphertext,
                encryption_version=record.encryption_version,
            ),
            owner_user_id=expected_context.owner_user_id,
            task_id=expected_context.task_id,
            node_id=expected_context.node_id,
            call_ref=expected_context.call_ref,
            state_kind=_TASK_PRIVATE_REMOTE_TASK_KIND,
            server_id=record.server_id,
            protocol_version=record.protocol_version,
        )
        remote_task_id = payload.get("remote_task_id")
        if not isinstance(remote_task_id, str) or not remote_task_id:
            raise CredentialSecurityError("mcp_task_private_decryption_failed")
        _validate_private_string_size(
            remote_task_id,
            maximum=MAX_REMOTE_TASK_ID_BYTES,
            error_code="mcp_task_private_decryption_failed",
        )
        return remote_task_id


def _record_matches_context(
    record: MCPSealedState | MCPRemoteTaskBinding,
    expected: MCPRecoveryCallContext,
) -> bool:
    return (
        record.owner_user_id == expected.owner_user_id
        and record.task_id == expected.task_id
        and record.node_id == expected.node_id
        and record.call_ref == expected.call_ref
    )


def _validate_private_string_size(value: Any, *, maximum: int, error_code: str) -> None:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > maximum:
        raise CredentialSecurityError(error_code)


def _credential_aad(owner_user_id: str, server_id: str, encryption_version: int) -> bytes:
    if encryption_version != CREDENTIAL_ENCRYPTION_VERSION:
        raise CredentialSecurityError("mcp_credential_encryption_version_unsupported")
    return _length_prefixed((b"mcp-user-credential", str(encryption_version).encode(), owner_user_id.encode(), server_id.encode()))


def _task_private_aad(
    *,
    owner_user_id: str,
    task_id: str,
    node_id: str,
    call_ref: str,
    state_kind: str,
    server_id: str,
    protocol_version: str,
    encryption_version: int,
) -> bytes:
    if encryption_version != CREDENTIAL_ENCRYPTION_VERSION:
        raise CredentialSecurityError("mcp_credential_encryption_version_unsupported")
    values = (
        owner_user_id,
        task_id,
        node_id,
        call_ref,
        state_kind,
        server_id,
        protocol_version,
    )
    if any(not isinstance(value, str) or not value for value in values):
        raise CredentialSecurityError("mcp_task_private_context_invalid")
    return _length_prefixed(
        (
            b"mcp-task-private-state",
            str(encryption_version).encode(),
            *(value.encode("utf-8") for value in values),
        )
    )


def _length_prefixed(parts: tuple[bytes, ...]) -> bytes:
    return b"".join(len(part).to_bytes(4, "big") + part for part in parts)


def _encode_credential(auth_type: str, values: Mapping[str, Any]) -> bytes:
    validated = _validate_credential(auth_type, values)
    payload = json.dumps(validated, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_CREDENTIAL_JSON_BYTES:
        raise CredentialSecurityError("mcp_credential_payload_too_large")
    return payload


def _validate_credential(auth_type: str, values: Any) -> dict[str, Any]:
    allowed = _ALLOWED_AUTH_FIELDS.get(auth_type)
    if allowed is None or not isinstance(values, Mapping) or set(values) != allowed:
        raise CredentialSecurityError("mcp_credential_payload_invalid")
    if auth_type in {"bearer", "api_key_header"}:
        field = next(iter(allowed))
        value = values.get(field)
        if not isinstance(value, str) or not value:
            raise CredentialSecurityError("mcp_credential_payload_invalid")
        return {field: value}
    header_values = values.get("values")
    if not isinstance(header_values, Mapping) or not header_values:
        raise CredentialSecurityError("mcp_credential_payload_invalid")
    normalized: dict[str, str] = {}
    for name, value in header_values.items():
        if not isinstance(name, str) or not isinstance(value, str) or not value:
            raise CredentialSecurityError("mcp_credential_payload_invalid")
        normalized[name] = value
    return {"values": normalized}


def _coerce_encrypted_record(record: Any) -> EncryptedCredential:
    try:
        if isinstance(record, Mapping):
            nonce = record.get("nonce", record.get("credential_nonce"))
            ciphertext = record.get("ciphertext", record.get("credential_ciphertext"))
            version = record["encryption_version"]
        else:
            nonce = getattr(record, "nonce", getattr(record, "credential_nonce", None))
            ciphertext = getattr(record, "ciphertext", getattr(record, "credential_ciphertext", None))
            version = record.encryption_version
        if not isinstance(nonce, bytes) or len(nonce) != _NONCE_BYTES or not isinstance(ciphertext, bytes):
            raise ValueError
        return EncryptedCredential(nonce=nonce, ciphertext=ciphertext, encryption_version=int(version))
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise CredentialSecurityError("mcp_credential_decryption_failed") from exc
