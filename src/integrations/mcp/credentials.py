from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import datetime, timezone
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from src.core.models import (
    MAFMasterKeyValidation,
    MCPRemoteTaskBinding,
    MCPMRTRRequestStateEvidence,
    MCPSealedState,
)
from src.integrations.mcp.cp7_artifacts import canonical_sha256
from src.integrations.master_key import (
    MasterKeyDomain,
    MasterKeyError,
    _DerivedDomainKey,
)


CREDENTIAL_ENCRYPTION_VERSION = 1
MAX_CREDENTIAL_JSON_BYTES = 16 * 1024
MAX_TASK_PRIVATE_JSON_BYTES = 64 * 1024
MAX_REQUEST_STATE_BYTES = 32 * 1024
MAX_REMOTE_TASK_ID_BYTES = 4 * 1024
_NONCE_BYTES = 12
_AES_GCM_TAG_BYTES = 16
_MASTER_KEY_SENTINEL_PLAINTEXT = b"maf-master-key-validation-v1"
_MASTER_KEY_SENTINEL_AAD = b"maf-master-key-validation\x00v1"
_ALLOWED_AUTH_FIELDS = {
    "bearer": frozenset({"token"}),
    "api_key_header": frozenset({"value"}),
    "static_headers": frozenset({"values"}),
}
_TASK_PRIVATE_REQUEST_STATE_KIND = "request_state"
_MRTR_REQUEST_STATE_EVIDENCE_SCHEMA_V2 = "maf.user_mcp.mrtr_request_state.v2"
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
    pending_action_id: str | None = None
    arguments_payload_ref: str | None = None
    arguments_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class SealedMasterKeySentinel:
    validation_nonce: bytes
    validation_ciphertext: bytes
    derivation_version: int = 1

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(validation_nonce=<redacted>, "
            "validation_ciphertext=<redacted>, "
            f"derivation_version={self.derivation_version})"
        )


@runtime_checkable
class CredentialSentinelStorage(Protocol):
    """Minimal storage boundary needed for atomic sentinel create-or-verify."""

    async def get_maf_master_key_validation(self) -> MAFMasterKeyValidation | None: ...

    async def create_or_get_maf_master_key_validation(
        self, record: MAFMasterKeyValidation
    ) -> MAFMasterKeyValidation: ...


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


class MCPCredentialCipher:
    """Encrypts only owner-scoped MCP credentials."""

    __slots__ = ("_cipher",)

    def __init__(self, key: _DerivedDomainKey) -> None:
        if not isinstance(key, _DerivedDomainKey):
            raise MasterKeyError("maf_key_domain_invalid")
        self._cipher = AESGCM(key._consume_for(MasterKeyDomain.MCP_CREDENTIAL))

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
        return EncryptedCredential(
            nonce=nonce,
            ciphertext=ciphertext,
            encryption_version=encryption_version,
        )

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
                _credential_aad(
                    owner_user_id,
                    server_id,
                    encrypted.encryption_version,
                ),
            )
            if len(plaintext) > MAX_CREDENTIAL_JSON_BYTES:
                raise CredentialSecurityError("mcp_credential_payload_too_large")
            decoded = json.loads(plaintext.decode("utf-8"))
            return _validate_credential(auth_type, decoded)
        except (
            CredentialSecurityError,
            InvalidTag,
            UnicodeDecodeError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ) as exc:
            raise CredentialSecurityError("mcp_credential_decryption_failed") from exc

    def __reduce__(self) -> object:
        raise TypeError("MCP credential ciphers cannot be serialized")

    def __reduce_ex__(self, _protocol: int) -> object:
        raise TypeError("MCP credential ciphers cannot be serialized")


class MCPRecoveryCipher:
    """Encrypts only task-private MCP recovery payloads."""

    __slots__ = ("_cipher",)

    def __init__(self, key: _DerivedDomainKey) -> None:
        if not isinstance(key, _DerivedDomainKey):
            raise MasterKeyError("maf_key_domain_invalid")
        self._cipher = AESGCM(key._consume_for(MasterKeyDomain.MCP_RECOVERY))

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

    def __reduce__(self) -> object:
        raise TypeError("MCP recovery ciphers cannot be serialized")

    def __reduce_ex__(self, _protocol: int) -> object:
        raise TypeError("MCP recovery ciphers cannot be serialized")


class MCPAuditReferenceSigner:
    """Produces only context-bound MCP audit references."""

    __slots__ = ("_key",)

    def __init__(self, key: _DerivedDomainKey) -> None:
        if not isinstance(key, _DerivedDomainKey):
            raise MasterKeyError("maf_key_domain_invalid")
        self._key = key._consume_for(MasterKeyDomain.MCP_AUDIT_REFERENCE)

    def safe_reference(self, value: str, *, context: str) -> str:
        subject = str(value).strip()
        safe_context = str(context).strip()
        if not subject or not safe_context:
            raise CredentialSecurityError("mcp_rollout_audit_identity_invalid")
        return hmac.new(
            self._key,
            f"{safe_context}\0{subject}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def safe_owner_reference(self, owner_user_id: str, *, context: str) -> str:
        return self.safe_reference(owner_user_id, context=context)

    def verify_reference(self, value: str, reference: str, *, context: str) -> bool:
        try:
            expected = self.safe_reference(value, context=context)
        except CredentialSecurityError:
            return False
        return hmac.compare_digest(expected, str(reference))

    def __reduce__(self) -> object:
        raise TypeError("MCP audit reference signers cannot be serialized")

    def __reduce_ex__(self, _protocol: int) -> object:
        raise TypeError("MCP audit reference signers cannot be serialized")


class MasterKeySentinelCipher:
    """Seals and verifies only the master-key validation sentinel."""

    __slots__ = ("_cipher",)

    def __init__(self, key: _DerivedDomainKey) -> None:
        if not isinstance(key, _DerivedDomainKey):
            raise MasterKeyError("maf_key_domain_invalid")
        self._cipher = AESGCM(key._consume_for(MasterKeyDomain.KEY_VALIDATION))

    def seal(self) -> SealedMasterKeySentinel:
        nonce = os.urandom(_NONCE_BYTES)
        return SealedMasterKeySentinel(
            validation_nonce=nonce,
            validation_ciphertext=self._cipher.encrypt(
                nonce,
                _MASTER_KEY_SENTINEL_PLAINTEXT,
                _MASTER_KEY_SENTINEL_AAD,
            ),
        )

    def verify(self, record: SealedMasterKeySentinel | Mapping[str, Any] | Any) -> None:
        try:
            if isinstance(record, Mapping):
                nonce = record["validation_nonce"]
                ciphertext = record["validation_ciphertext"]
                derivation_version = int(record["derivation_version"])
            else:
                nonce = record.validation_nonce
                ciphertext = record.validation_ciphertext
                derivation_version = int(record.derivation_version)
            if (
                derivation_version != 1
                or not isinstance(nonce, bytes)
                or len(nonce) != _NONCE_BYTES
                or not isinstance(ciphertext, bytes)
            ):
                raise ValueError
            plaintext = self._cipher.decrypt(
                nonce,
                ciphertext,
                _MASTER_KEY_SENTINEL_AAD,
            )
        except (
            AttributeError,
            InvalidTag,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            raise CredentialSecurityError("maf_master_key_mismatch") from exc
        if plaintext != _MASTER_KEY_SENTINEL_PLAINTEXT:
            raise CredentialSecurityError("maf_master_key_mismatch")

    async def create_or_verify_sentinel(
        self, storage: CredentialSentinelStorage
    ) -> None:
        try:
            current = await storage.get_maf_master_key_validation()
        except Exception as exc:
            raise CredentialSecurityError(
                "maf_master_key_validation_unavailable"
            ) from exc
        if current is not None:
            self.verify(current)
            return

        sealed = self.seal()
        candidate = MAFMasterKeyValidation(
            singleton_key=1,
            validation_nonce=sealed.validation_nonce,
            validation_ciphertext=sealed.validation_ciphertext,
            derivation_version=sealed.derivation_version,
            created_at=datetime.now(timezone.utc),
        )
        try:
            winner = await storage.create_or_get_maf_master_key_validation(candidate)
        except Exception as exc:
            raise CredentialSecurityError(
                "maf_master_key_validation_unavailable"
            ) from exc
        self.verify(winner)

    def __reduce__(self) -> object:
        raise TypeError("master key sentinel ciphers cannot be serialized")

    def __reduce_ex__(self, _protocol: int) -> object:
        raise TypeError("master key sentinel ciphers cannot be serialized")


class MCPRecoveryService:
    """Durable, task-private storage for opaque MCP continuation values."""

    def __init__(
        self,
        storage: MCPRecoveryStorage,
        cipher: MCPRecoveryCipher,
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
        input_requests: Mapping[str, Mapping[str, Any]],
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
                "schema": _MRTR_REQUEST_STATE_EVIDENCE_SCHEMA_V2,
                "request_state": request_state,
                "tool_name": tool_name,
                "arguments_sha256": (
                    context.arguments_sha256 or canonical_sha256(dict(arguments))
                ),
                "input_requests": {
                    str(key): dict(value)
                    for key, value in input_requests.items()
                },
                "pending_action_id": context.pending_action_id,
                "arguments_payload_ref": context.arguments_payload_ref,
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
            "schema": _MRTR_REQUEST_STATE_EVIDENCE_SCHEMA_V2,
            "request_state": request_state,
            "tool_name": tool_name,
            "arguments_sha256": (
                context.arguments_sha256 or canonical_sha256(dict(arguments))
            ),
            "input_requests": {
                str(key): dict(value) for key, value in input_requests.items()
            },
            "pending_action_id": context.pending_action_id,
            "arguments_payload_ref": context.arguments_payload_ref,
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
        v2 = payload.get("schema") == _MRTR_REQUEST_STATE_EVIDENCE_SCHEMA_V2
        if v2 and (
            set(payload)
            != {
                "schema",
                "request_state",
                "tool_name",
                "arguments_sha256",
                "input_requests",
                "pending_action_id",
                "arguments_payload_ref",
            }
            or payload.get("tool_name") != tool_name
            or payload.get("arguments_sha256")
            != (
                expected_context.arguments_sha256
                or canonical_sha256(dict(arguments))
            )
            or payload.get("pending_action_id") != expected_context.pending_action_id
            or payload.get("arguments_payload_ref")
            != expected_context.arguments_payload_ref
            or not isinstance(payload.get("input_requests"), dict)
        ):
            raise CredentialSecurityError("mcp_recovery_request_mismatch")
        if not v2 and (
            set(payload) != {"request_state", "tool_name", "arguments"}
            or payload.get("tool_name") != tool_name
            or payload.get("arguments") != dict(arguments)
        ):
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


class MCPRequestStateEvidenceAuthority:
    def __init__(self, cipher: MCPRecoveryCipher) -> None:
        if not isinstance(cipher, MCPRecoveryCipher):
            raise TypeError("MCP recovery cipher is required")
        self._cipher = cipher

    def read(
        self,
        record: MCPSealedState,
        *,
        server_id: str,
        protocol_version: str,
    ) -> MCPMRTRRequestStateEvidence:
        if record.state_kind != _TASK_PRIVATE_REQUEST_STATE_KIND:
            raise CredentialSecurityError("mcp_mrtr_evidence_kind_invalid")
        payload = self._cipher.unseal_task_private_payload(
            EncryptedCredential(
                nonce=record.nonce,
                ciphertext=record.ciphertext,
                encryption_version=record.encryption_version,
            ),
            owner_user_id=record.owner_user_id,
            task_id=record.task_id,
            node_id=record.node_id,
            call_ref=record.call_ref,
            state_kind=record.state_kind,
            server_id=server_id,
            protocol_version=protocol_version,
        )
        if set(payload) != {
            "schema",
            "request_state",
            "tool_name",
            "arguments_sha256",
            "input_requests",
            "pending_action_id",
            "arguments_payload_ref",
        } or payload.get("schema") != _MRTR_REQUEST_STATE_EVIDENCE_SCHEMA_V2:
            raise CredentialSecurityError("mcp_mrtr_evidence_schema_invalid")
        request_state = payload.get("request_state")
        tool_name = payload.get("tool_name")
        arguments_sha256 = payload.get("arguments_sha256")
        input_requests = payload.get("input_requests")
        pending_action_id = payload.get("pending_action_id")
        arguments_payload_ref = payload.get("arguments_payload_ref")
        if (
            not isinstance(request_state, str)
            or len(request_state.encode("utf-8")) > MAX_REQUEST_STATE_BYTES
            or not isinstance(tool_name, str)
            or not tool_name
            or not isinstance(arguments_sha256, str)
            or (
                not (
                    len(arguments_sha256) == 64
                    and all(character in "0123456789abcdef" for character in arguments_sha256)
                )
                and not (
                    len(arguments_sha256) == 71
                    and arguments_sha256.startswith("sha256:")
                    and all(
                        character in "0123456789abcdef"
                        for character in arguments_sha256[7:]
                    )
                )
            )
            or not isinstance(input_requests, dict)
            or any(
                not isinstance(key, str) or not isinstance(value, dict)
                for key, value in input_requests.items()
            )
            or not isinstance(pending_action_id, str)
            or not pending_action_id
            or not isinstance(arguments_payload_ref, str)
            or not arguments_payload_ref
        ):
            raise CredentialSecurityError("mcp_mrtr_evidence_payload_invalid")
        return MCPMRTRRequestStateEvidence(
            sealed_state_ref=record.sealed_state_ref,
            owner_user_id=record.owner_user_id,
            task_id=record.task_id,
            node_id=record.node_id,
            call_ref=record.call_ref,
            request_state=request_state,
            tool_name=tool_name,
            arguments_sha256=arguments_sha256,
            input_requests={
                str(key): dict(value) for key, value in input_requests.items()
            },
            pending_action_id=pending_action_id,
            arguments_payload_ref=arguments_payload_ref,
        )


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
