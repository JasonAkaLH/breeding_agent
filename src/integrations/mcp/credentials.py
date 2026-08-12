from __future__ import annotations

import base64
import binascii
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

from src.core.models import MCPCredentialKeyValidation


MCP_CREDENTIAL_KEY_FILE_ENV = "MCP_CREDENTIAL_KEY_FILE"
CREDENTIAL_ENCRYPTION_VERSION = 1
MAX_CREDENTIAL_JSON_BYTES = 16 * 1024
_NONCE_BYTES = 12
_SENTINEL_PLAINTEXT = b"mcp-credential-key-validation-v1"
_SENTINEL_AAD = b"mcp-credential-key-validation\x00v1"
_ALLOWED_AUTH_FIELDS = {
    "bearer": frozenset({"token"}),
    "api_key_header": frozenset({"value"}),
    "static_headers": frozenset({"values"}),
}


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


@runtime_checkable
class CredentialSentinelStorage(Protocol):
    """Minimal storage boundary needed for atomic sentinel create-or-verify."""

    async def get_mcp_credential_key_validation(self) -> MCPCredentialKeyValidation | None: ...

    async def create_or_get_mcp_credential_key_validation(
        self, record: MCPCredentialKeyValidation
    ) -> MCPCredentialKeyValidation: ...


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


def _credential_aad(owner_user_id: str, server_id: str, encryption_version: int) -> bytes:
    if encryption_version != CREDENTIAL_ENCRYPTION_VERSION:
        raise CredentialSecurityError("mcp_credential_encryption_version_unsupported")
    return _length_prefixed((b"mcp-user-credential", str(encryption_version).encode(), owner_user_id.encode(), server_id.encode()))


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
