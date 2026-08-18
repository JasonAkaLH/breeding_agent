from __future__ import annotations

import asyncio
import contextlib
import errno
import hashlib
import json
import os
import re
import secrets
import stat
import struct
import threading
from collections.abc import Awaitable, Callable, Collection, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from src.core.models import MCPPendingActionPayloadSnapshot
from src.integrations.master_key import (
    MasterKeyDomain,
    MasterKeyError,
    _DerivedDomainKey,
)
from src.integrations.mcp.cp7_artifacts import (
    CP7ArtifactValidationError,
    canonical_json_bytes,
    parse_canonical_json_bytes,
)


MAX_PENDING_ACTION_ARGUMENT_BYTES = 32 * 1024 * 1024
PENDING_ACTION_PAYLOAD_ENCRYPTION_VERSION = 1
PENDING_ACTION_PAYLOAD_ORPHAN_GRACE = timedelta(hours=24)

_MAGIC = b"MAFMPA1\0"
_NONCE_BYTES = 12
_TAG_BYTES = 16
_HEADER = struct.Struct(">8sH12sQ")
_MAX_FILE_BYTES = _HEADER.size + MAX_PENDING_ACTION_ARGUMENT_BYTES + _TAG_BYTES
_AAD_PREFIX = b"pending_action_payload\0v1\0"
_PAYLOAD_REF_RE = re.compile(r"^mcp-action-payload-v1-[0-9a-f]{64}$")
_TEMP_FILE_RE = re.compile(r"^\.pending-action-[0-9a-f]{32}\.tmp$")


class MCPPendingActionPayloadError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class MCPPendingActionPayloadIdentity:
    action_id: str
    owner_user_id: str
    task_id: str
    node_id: str
    server_id: str
    tool_name: str
    server_config_version: int
    server_security_version: int
    input_schema_sha256: str
    arguments_sha256: str

    def __post_init__(self) -> None:
        for value in (
            self.action_id,
            self.owner_user_id,
            self.task_id,
            self.node_id,
            self.server_id,
            self.tool_name,
            self.input_schema_sha256,
            self.arguments_sha256,
        ):
            if not isinstance(value, str) or not value or value.strip() != value:
                raise MCPPendingActionPayloadError(
                    "mcp_pending_action_payload_identity_invalid"
                )
        for value in (self.server_config_version, self.server_security_version):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise MCPPendingActionPayloadError(
                    "mcp_pending_action_payload_identity_invalid"
                )

    def aad_values(self) -> dict[str, object]:
        return {
            "action_id": self.action_id,
            "arguments_sha256": self.arguments_sha256,
            "input_schema_sha256": self.input_schema_sha256,
            "node_id": self.node_id,
            "owner_user_id": self.owner_user_id,
            "server_config_version": self.server_config_version,
            "server_id": self.server_id,
            "server_security_version": self.server_security_version,
            "task_id": self.task_id,
            "tool_name": self.tool_name,
        }


@dataclass(frozen=True, slots=True)
class MCPValidatedPendingActionPayload:
    snapshot: MCPPendingActionPayloadSnapshot
    arguments: Mapping[str, Any]

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(snapshot={self.snapshot!r}, "
            "arguments=<redacted>)"
        )


@dataclass(frozen=True, slots=True)
class MCPPendingActionPayloadDeletionEvidence:
    action_id: str
    payload_ref: str
    action_status: str
    call_status: str
    result_receipt_id: str | None = None
    unknown_projection_id: str | None = None

    def authorizes_deletion(self) -> bool:
        ordinary_terminal = (
            self.call_status in {"completed", "failed", "cancelled"}
            and isinstance(self.result_receipt_id, str)
            and bool(self.result_receipt_id)
            and self.unknown_projection_id is None
        )
        unknown_terminal = (
            self.call_status == "unknown"
            and isinstance(self.unknown_projection_id, str)
            and bool(self.unknown_projection_id)
            and self.result_receipt_id is None
        )
        return self.action_status == "consumed" and (
            ordinary_terminal or unknown_terminal
        )


@dataclass(slots=True)
class _HeldPayload:
    descriptor: int
    path: Path
    identity: tuple[int, ...]
    root_identity: tuple[int, ...]
    snapshot: MCPPendingActionPayloadSnapshot


class MCPPendingActionPayloadCipher:
    __slots__ = ("_cipher",)

    def __init__(self, key: _DerivedDomainKey) -> None:
        if not isinstance(key, _DerivedDomainKey):
            raise MasterKeyError("maf_key_domain_invalid")
        self._cipher = AESGCM(key._consume_for(MasterKeyDomain.MCP_RECOVERY))

    def seal(
        self,
        identity: MCPPendingActionPayloadIdentity,
        canonical_arguments: bytes,
    ) -> bytes:
        _parse_arguments(canonical_arguments)
        return self._seal_validated(identity, canonical_arguments)

    def _seal_validated(
        self,
        identity: MCPPendingActionPayloadIdentity,
        canonical_arguments: bytes,
    ) -> bytes:
        nonce = os.urandom(_NONCE_BYTES)
        ciphertext = self._cipher.encrypt(
            nonce,
            canonical_arguments,
            _aad(identity, PENDING_ACTION_PAYLOAD_ENCRYPTION_VERSION),
        )
        return _HEADER.pack(
            _MAGIC,
            PENDING_ACTION_PAYLOAD_ENCRYPTION_VERSION,
            nonce,
            len(ciphertext),
        ) + ciphertext

    def unseal(
        self,
        identity: MCPPendingActionPayloadIdentity,
        payload: bytes,
    ) -> tuple[bytes, Mapping[str, Any]]:
        plaintext = self._decrypt(identity, payload)
        return plaintext, _parse_authenticated_arguments(plaintext)

    def _decrypt(
        self,
        identity: MCPPendingActionPayloadIdentity,
        payload: bytes,
    ) -> bytes:
        if not isinstance(payload, bytes) or len(payload) < _HEADER.size + _TAG_BYTES:
            raise MCPPendingActionPayloadError(
                "mcp_pending_action_payload_format_invalid"
            )
        try:
            magic, version, nonce, ciphertext_length = _HEADER.unpack_from(payload)
        except struct.error as exc:
            raise MCPPendingActionPayloadError(
                "mcp_pending_action_payload_format_invalid"
            ) from exc
        if (
            magic != _MAGIC
            or version != PENDING_ACTION_PAYLOAD_ENCRYPTION_VERSION
            or len(nonce) != _NONCE_BYTES
            or ciphertext_length < _TAG_BYTES
            or ciphertext_length > MAX_PENDING_ACTION_ARGUMENT_BYTES + _TAG_BYTES
            or len(payload) != _HEADER.size + ciphertext_length
        ):
            raise MCPPendingActionPayloadError(
                "mcp_pending_action_payload_format_invalid"
            )
        try:
            plaintext = self._cipher.decrypt(
                nonce,
                memoryview(payload)[_HEADER.size :],
                _aad(identity, version),
            )
        except (InvalidTag, TypeError, ValueError) as exc:
            raise MCPPendingActionPayloadError(
                "mcp_pending_action_payload_decryption_failed"
            ) from exc
        return plaintext

    def __reduce__(self) -> object:
        raise TypeError("MCP pending-action payload ciphers cannot be serialized")

    def __reduce_ex__(self, _protocol: int) -> object:
        raise TypeError("MCP pending-action payload ciphers cannot be serialized")


class MCPPendingActionPayloadStore:
    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        cipher: MCPPendingActionPayloadCipher,
        disk_available: Callable[[Path], bool] | None = None,
        gate_wait_interval_seconds: float = 1.0,
    ) -> None:
        if not isinstance(cipher, MCPPendingActionPayloadCipher):
            raise TypeError("pending-action payload cipher is required")
        if gate_wait_interval_seconds <= 0:
            raise ValueError("gate_wait_interval_seconds must be positive")
        self._root = Path(os.path.abspath(os.fspath(root)))
        self._cipher = cipher
        self._disk_available = disk_available or _default_disk_available
        self._gate_wait_interval_seconds = gate_wait_interval_seconds
        self._crypto_gate = asyncio.Semaphore(1)
        self._held: dict[str, list[_HeldPayload]] = {}
        self._held_lock = threading.Lock()
        _secure_root(self._root)

    @property
    def root(self) -> Path:
        return self._root

    async def seal(
        self,
        identity: MCPPendingActionPayloadIdentity,
        arguments: Mapping[str, Any],
        *,
        on_gate_wait: Callable[[], Awaitable[None]] | None = None,
    ) -> MCPPendingActionPayloadSnapshot:
        try:
            canonical_arguments = canonical_json_bytes(dict(arguments))
        except (CP7ArtifactValidationError, TypeError, ValueError) as exc:
            raise MCPPendingActionPayloadError(
                "mcp_pending_action_payload_invalid"
            ) from exc
        if len(canonical_arguments) > MAX_PENDING_ACTION_ARGUMENT_BYTES:
            raise MCPPendingActionPayloadError(
                "mcp_pending_action_payload_too_large"
            )
        payload_ref = pending_action_payload_ref(identity)
        path = self._path(payload_ref)
        if path.exists():
            return await self._adopt_existing(
                identity,
                canonical_arguments,
                payload_ref,
                on_gate_wait=on_gate_wait,
            )
        self._require_disk_capacity()
        blob = await self._run_crypto(
            lambda: self._cipher._seal_validated(identity, canonical_arguments),
            on_gate_wait=on_gate_wait,
        )
        self._require_disk_capacity()
        try:
            file_stat = await asyncio.to_thread(_publish_blob, self._root, path, blob)
        except FileExistsError:
            return await self._adopt_existing(
                identity,
                canonical_arguments,
                payload_ref,
                on_gate_wait=on_gate_wait,
            )
        except OSError as exc:
            if exc.errno in {errno.ENOSPC, errno.EDQUOT}:
                raise MCPPendingActionPayloadError(
                    "mcp_pending_action_payload_storage_exhausted"
                ) from exc
            raise MCPPendingActionPayloadError(
                "mcp_pending_action_payload_publish_failed"
            ) from exc
        self._require_disk_capacity()
        return _snapshot(
            identity,
            payload_ref,
            canonical_arguments,
            "sha256:" + hashlib.sha256(blob).hexdigest(),
            file_stat,
        )

    @asynccontextmanager
    async def open_validated(
        self,
        identity: MCPPendingActionPayloadIdentity,
        payload_ref: str,
        *,
        expected_snapshot: MCPPendingActionPayloadSnapshot | None = None,
        on_gate_wait: Callable[[], Awaitable[None]] | None = None,
    ) -> AsyncIterator[MCPValidatedPendingActionPayload]:
        _validate_payload_ref(payload_ref)
        held, arguments, _canonical_arguments = await self._run_crypto(
            lambda: _open_payload(
                self._root,
                self._path(payload_ref),
                identity,
                payload_ref,
                self._cipher,
            ),
            on_gate_wait=on_gate_wait,
        )
        if expected_snapshot is not None and held.snapshot != expected_snapshot:
            os.close(held.descriptor)
            raise MCPPendingActionPayloadError(
                "mcp_pending_action_payload_snapshot_conflict"
            )
        with self._held_lock:
            self._held.setdefault(payload_ref, []).append(held)
        try:
            yield MCPValidatedPendingActionPayload(
                snapshot=held.snapshot,
                arguments=arguments,
            )
        finally:
            with self._held_lock:
                entries = self._held.get(payload_ref, [])
                if held in entries:
                    entries.remove(held)
                if not entries:
                    self._held.pop(payload_ref, None)
            os.close(held.descriptor)

    def revalidate(
        self, snapshot: MCPPendingActionPayloadSnapshot
    ) -> MCPPendingActionPayloadSnapshot:
        with self._held_lock:
            entries = tuple(self._held.get(snapshot.arguments_payload_ref, ()))
        for held in entries:
            if held.snapshot != snapshot:
                continue
            try:
                opened = os.fstat(held.descriptor)
                current = os.stat(held.path, follow_symlinks=False)
                root = os.stat(self._root, follow_symlinks=False)
            except OSError as exc:
                raise MCPPendingActionPayloadError(
                    "mcp_pending_action_payload_descriptor_conflict"
                ) from exc
            if (
                _stat_identity(opened) != held.identity
                or _stat_identity(current) != held.identity
                or _directory_identity(root) != held.root_identity
            ):
                raise MCPPendingActionPayloadError(
                    "mcp_pending_action_payload_descriptor_conflict"
                )
            return snapshot
        raise MCPPendingActionPayloadError(
            "mcp_pending_action_payload_descriptor_not_held"
        )

    async def cleanup_orphans(
        self,
        *,
        referenced_payload_refs: Collection[str],
        now: datetime,
    ) -> int:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("pending-action orphan cleanup time must be timezone-aware")
        referenced = {str(value) for value in referenced_payload_refs}
        for value in referenced:
            _validate_payload_ref(value)
        return await asyncio.to_thread(
            _cleanup_orphans,
            self._root,
            referenced,
            now.astimezone(timezone.utc),
        )

    async def delete_with_terminal_evidence(
        self,
        identity: MCPPendingActionPayloadIdentity,
        snapshot: MCPPendingActionPayloadSnapshot,
        evidence: MCPPendingActionPayloadDeletionEvidence,
    ) -> bool:
        if (
            evidence.action_id != identity.action_id
            or evidence.payload_ref != snapshot.arguments_payload_ref
            or not evidence.authorizes_deletion()
        ):
            return False
        async with self.open_validated(
            identity,
            snapshot.arguments_payload_ref,
            expected_snapshot=snapshot,
        ) as opened:
            self.revalidate(opened.snapshot)
            await asyncio.to_thread(
                _unlink_exact_payload,
                self._root,
                self._path(snapshot.arguments_payload_ref),
                snapshot,
            )
        return True

    async def _adopt_existing(
        self,
        identity: MCPPendingActionPayloadIdentity,
        canonical_arguments: bytes,
        payload_ref: str,
        *,
        on_gate_wait: Callable[[], Awaitable[None]] | None,
    ) -> MCPPendingActionPayloadSnapshot:
        held, _existing_arguments, existing_canonical = await self._run_crypto(
            lambda: _open_payload(
                self._root,
                self._path(payload_ref),
                identity,
                payload_ref,
                self._cipher,
            ),
            on_gate_wait=on_gate_wait,
        )
        try:
            if existing_canonical != canonical_arguments:
                raise MCPPendingActionPayloadError(
                    "mcp_pending_action_payload_conflict"
                )
            return held.snapshot
        finally:
            os.close(held.descriptor)

    async def _run_crypto(
        self,
        operation: Callable[[], Any],
        *,
        on_gate_wait: Callable[[], Awaitable[None]] | None,
    ) -> Any:
        while True:
            try:
                await asyncio.wait_for(
                    self._crypto_gate.acquire(),
                    timeout=self._gate_wait_interval_seconds,
                )
                break
            except TimeoutError:
                if on_gate_wait is not None:
                    await on_gate_wait()
        try:
            return await asyncio.to_thread(operation)
        finally:
            self._crypto_gate.release()

    def _path(self, payload_ref: str) -> Path:
        _validate_payload_ref(payload_ref)
        return self._root / f"{payload_ref}.bin"

    def _require_disk_capacity(self) -> None:
        try:
            available = bool(self._disk_available(self._root))
        except Exception:
            available = False
        if not available:
            raise MCPPendingActionPayloadError(
                "mcp_pending_action_payload_capacity_unavailable"
            )


def pending_action_payload_ref(identity: MCPPendingActionPayloadIdentity) -> str:
    digest = hashlib.sha256(_aad(identity, PENDING_ACTION_PAYLOAD_ENCRYPTION_VERSION))
    return f"mcp-action-payload-v1-{digest.hexdigest()}"


def _aad(identity: MCPPendingActionPayloadIdentity, version: int) -> bytes:
    return _AAD_PREFIX + canonical_json_bytes(
        {"encryption_version": version, **identity.aad_values()}
    )


def _parse_arguments(content: bytes) -> Mapping[str, Any]:
    if not isinstance(content, bytes) or len(content) > MAX_PENDING_ACTION_ARGUMENT_BYTES:
        raise MCPPendingActionPayloadError(
            "mcp_pending_action_payload_too_large"
        )
    try:
        value = parse_canonical_json_bytes(content)
    except CP7ArtifactValidationError as exc:
        raise MCPPendingActionPayloadError(
            "mcp_pending_action_payload_invalid"
        ) from exc
    if not isinstance(value, Mapping):
        raise MCPPendingActionPayloadError(
            "mcp_pending_action_payload_invalid"
        )
    return dict(value)


def _parse_authenticated_arguments(content: bytes) -> Mapping[str, Any]:
    if len(content) > MAX_PENDING_ACTION_ARGUMENT_BYTES:
        raise MCPPendingActionPayloadError(
            "mcp_pending_action_payload_too_large"
        )
    try:
        value = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise MCPPendingActionPayloadError(
            "mcp_pending_action_payload_invalid"
        ) from exc
    if not isinstance(value, Mapping):
        raise MCPPendingActionPayloadError(
            "mcp_pending_action_payload_invalid"
        )
    return dict(value)


def _snapshot(
    identity: MCPPendingActionPayloadIdentity,
    payload_ref: str,
    canonical_arguments: bytes,
    file_sha256: str,
    file_stat: os.stat_result,
) -> MCPPendingActionPayloadSnapshot:
    return MCPPendingActionPayloadSnapshot(
        action_id=identity.action_id,
        owner_user_id=identity.owner_user_id,
        task_id=identity.task_id,
        node_id=identity.node_id,
        server_id=identity.server_id,
        tool_name=identity.tool_name,
        arguments_sha256=identity.arguments_sha256,
        arguments_payload_ref=payload_ref,
        payload_file_sha256=file_sha256,
        payload_size_bytes=len(canonical_arguments),
        encryption_version=PENDING_ACTION_PAYLOAD_ENCRYPTION_VERSION,
        server_config_version=identity.server_config_version,
        server_security_version=identity.server_security_version,
        input_schema_sha256=identity.input_schema_sha256,
        file_device=file_stat.st_dev,
        file_inode=file_stat.st_ino,
        file_mode=stat.S_IMODE(file_stat.st_mode),
        file_owner_uid=file_stat.st_uid,
    )


def _open_payload(
    root: Path,
    path: Path,
    identity: MCPPendingActionPayloadIdentity,
    payload_ref: str,
    cipher: MCPPendingActionPayloadCipher,
) -> tuple[_HeldPayload, Mapping[str, Any], bytes]:
    root_descriptor = -1
    descriptor = -1
    try:
        root_descriptor = os.open(
            root,
            os.O_RDONLY
            | _require_flag("O_DIRECTORY")
            | _require_flag("O_NOFOLLOW")
            | int(getattr(os, "O_CLOEXEC", 0)),
        )
        root_stat = os.fstat(root_descriptor)
        _validate_root_stat(root_stat)
        before = os.stat(path.name, dir_fd=root_descriptor, follow_symlinks=False)
        descriptor = os.open(
            path.name,
            os.O_RDONLY
            | _require_flag("O_NOFOLLOW")
            | int(getattr(os, "O_CLOEXEC", 0)),
            dir_fd=root_descriptor,
        )
        opened = os.fstat(descriptor)
        _validate_payload_file_stat(opened)
        if _stat_identity(before) != _stat_identity(opened):
            raise MCPPendingActionPayloadError(
                "mcp_pending_action_payload_file_unsafe"
            )
        blob = _read_exact(descriptor, opened.st_size)
        after = os.fstat(descriptor)
        after_path = os.stat(
            path.name, dir_fd=root_descriptor, follow_symlinks=False
        )
        if (
            _stat_identity(opened) != _stat_identity(after)
            or _stat_identity(after) != _stat_identity(after_path)
        ):
            raise MCPPendingActionPayloadError(
                "mcp_pending_action_payload_file_unsafe"
            )
        file_sha256 = "sha256:" + hashlib.sha256(blob).hexdigest()
        canonical_arguments = cipher._decrypt(identity, blob)
        del blob
        arguments = _parse_authenticated_arguments(canonical_arguments)
        snapshot = _snapshot(
            identity, payload_ref, canonical_arguments, file_sha256, after
        )
        held = _HeldPayload(
            descriptor=descriptor,
            path=path,
            identity=_stat_identity(after),
            root_identity=_directory_identity(root_stat),
            snapshot=snapshot,
        )
        descriptor = -1
        return held, arguments, canonical_arguments
    except MCPPendingActionPayloadError:
        raise
    except OSError as exc:
        raise MCPPendingActionPayloadError(
            "mcp_pending_action_payload_file_unsafe"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if root_descriptor >= 0:
            os.close(root_descriptor)


def _publish_blob(root: Path, path: Path, blob: bytes) -> os.stat_result:
    root_descriptor = os.open(
        root,
        os.O_RDONLY
        | _require_flag("O_DIRECTORY")
        | _require_flag("O_NOFOLLOW")
        | int(getattr(os, "O_CLOEXEC", 0)),
    )
    temporary_name = f".pending-action-{secrets.token_hex(16)}.tmp"
    descriptor = -1
    try:
        _validate_root_stat(os.fstat(root_descriptor))
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | _require_flag("O_NOFOLLOW")
            | int(getattr(os, "O_CLOEXEC", 0)),
            0o600,
            dir_fd=root_descriptor,
        )
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, blob)
        os.fsync(descriptor)
        _validate_payload_file_stat(os.fstat(descriptor))
        os.link(
            temporary_name,
            path.name,
            src_dir_fd=root_descriptor,
            dst_dir_fd=root_descriptor,
            follow_symlinks=False,
        )
        os.fsync(root_descriptor)
        os.unlink(temporary_name, dir_fd=root_descriptor)
        os.fsync(root_descriptor)
        settled = os.fstat(descriptor)
        _validate_payload_file_stat(settled)
        published = os.stat(path.name, dir_fd=root_descriptor, follow_symlinks=False)
        if _stat_identity(settled) != _stat_identity(published):
            raise MCPPendingActionPayloadError(
                "mcp_pending_action_payload_publish_failed"
            )
        return published
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary_name, dir_fd=root_descriptor)
            os.fsync(root_descriptor)
        os.close(root_descriptor)


def _cleanup_orphans(
    root: Path,
    referenced: set[str],
    now: datetime,
) -> int:
    root_descriptor = os.open(
        root,
        os.O_RDONLY
        | _require_flag("O_DIRECTORY")
        | _require_flag("O_NOFOLLOW")
        | int(getattr(os, "O_CLOEXEC", 0)),
    )
    removed = 0
    cutoff = (now - PENDING_ACTION_PAYLOAD_ORPHAN_GRACE).timestamp()
    try:
        _validate_root_stat(os.fstat(root_descriptor))
        for entry in sorted(os.scandir(root_descriptor), key=lambda item: item.name):
            if _TEMP_FILE_RE.fullmatch(entry.name) is not None:
                file_stat = entry.stat(follow_symlinks=False)
                _validate_temporary_file_stat(file_stat)
                if file_stat.st_mtime <= cutoff:
                    os.unlink(entry.name, dir_fd=root_descriptor)
                    os.fsync(root_descriptor)
                    removed += 1
                continue
            if not entry.name.endswith(".bin"):
                raise MCPPendingActionPayloadError(
                    "mcp_pending_action_payload_inventory_invalid"
                )
            payload_ref = entry.name.removesuffix(".bin")
            _validate_payload_ref(payload_ref)
            file_stat = entry.stat(follow_symlinks=False)
            _validate_payload_file_stat(file_stat)
            if payload_ref in referenced or file_stat.st_mtime > cutoff:
                continue
            os.unlink(entry.name, dir_fd=root_descriptor)
            os.fsync(root_descriptor)
            removed += 1
        return removed
    finally:
        os.close(root_descriptor)


def _unlink_exact_payload(
    root: Path,
    path: Path,
    snapshot: MCPPendingActionPayloadSnapshot,
) -> None:
    root_descriptor = os.open(
        root,
        os.O_RDONLY
        | _require_flag("O_DIRECTORY")
        | _require_flag("O_NOFOLLOW")
        | int(getattr(os, "O_CLOEXEC", 0)),
    )
    try:
        _validate_root_stat(os.fstat(root_descriptor))
        current = os.stat(path.name, dir_fd=root_descriptor, follow_symlinks=False)
        _validate_payload_file_stat(current)
        if (
            current.st_dev != snapshot.file_device
            or current.st_ino != snapshot.file_inode
            or stat.S_IMODE(current.st_mode) != snapshot.file_mode
            or current.st_uid != snapshot.file_owner_uid
        ):
            raise MCPPendingActionPayloadError(
                "mcp_pending_action_payload_delete_conflict"
            )
        os.unlink(path.name, dir_fd=root_descriptor)
        os.fsync(root_descriptor)
    finally:
        os.close(root_descriptor)


def _secure_root(root: Path) -> None:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    before = os.stat(root, follow_symlinks=False)
    if not stat.S_ISDIR(before.st_mode) or before.st_uid != os.getuid():
        raise MCPPendingActionPayloadError(
            "mcp_pending_action_payload_root_unsafe"
        )
    os.chmod(root, 0o700, follow_symlinks=False)
    after = os.stat(root, follow_symlinks=False)
    if _directory_identity(before)[:2] != _directory_identity(after)[:2]:
        raise MCPPendingActionPayloadError(
            "mcp_pending_action_payload_root_unsafe"
        )
    _validate_root_stat(after)


def _validate_root_stat(value: os.stat_result) -> None:
    if (
        not stat.S_ISDIR(value.st_mode)
        or value.st_uid != os.getuid()
        or stat.S_IMODE(value.st_mode) != 0o700
    ):
        raise MCPPendingActionPayloadError(
            "mcp_pending_action_payload_root_unsafe"
        )


def _validate_payload_file_stat(value: os.stat_result) -> None:
    if (
        not stat.S_ISREG(value.st_mode)
        or value.st_uid != os.getuid()
        or stat.S_IMODE(value.st_mode) != 0o600
        or value.st_nlink != 1
        or value.st_size < _HEADER.size + _TAG_BYTES
        or value.st_size > _MAX_FILE_BYTES
    ):
        raise MCPPendingActionPayloadError(
            "mcp_pending_action_payload_file_unsafe"
        )


def _validate_temporary_file_stat(value: os.stat_result) -> None:
    if (
        not stat.S_ISREG(value.st_mode)
        or value.st_uid != os.getuid()
        or stat.S_IMODE(value.st_mode) != 0o600
        or value.st_nlink != 1
        or value.st_size > _MAX_FILE_BYTES
    ):
        raise MCPPendingActionPayloadError(
            "mcp_pending_action_payload_file_unsafe"
        )


def _stat_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_uid,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _directory_identity(value: os.stat_result) -> tuple[int, ...]:
    return (value.st_dev, value.st_ino, value.st_mode, value.st_uid)


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError(errno.EIO, "pending-action payload write did not progress")
        view = view[written:]


def _read_exact(descriptor: int, size: int) -> bytes:
    parts: list[bytes] = []
    remaining = size
    while remaining:
        chunk = os.read(descriptor, min(remaining, 1024 * 1024))
        if not chunk:
            raise MCPPendingActionPayloadError(
                "mcp_pending_action_payload_file_truncated"
            )
        parts.append(chunk)
        remaining -= len(chunk)
    if os.read(descriptor, 1):
        raise MCPPendingActionPayloadError(
            "mcp_pending_action_payload_file_trailing_bytes"
        )
    return b"".join(parts)


def _validate_payload_ref(value: str) -> None:
    if not isinstance(value, str) or _PAYLOAD_REF_RE.fullmatch(value) is None:
        raise MCPPendingActionPayloadError(
            "mcp_pending_action_payload_ref_invalid"
        )


def _require_flag(name: str) -> int:
    value = getattr(os, name, None)
    if value is None:
        raise MCPPendingActionPayloadError(
            "mcp_pending_action_payload_platform_unsupported"
        )
    return int(value)


def _default_disk_available(path: Path) -> bool:
    filesystem = os.statvfs(path)
    return filesystem.f_bavail * filesystem.f_frsize > _MAX_FILE_BYTES


__all__ = [
    "MAX_PENDING_ACTION_ARGUMENT_BYTES",
    "MCPPendingActionPayloadCipher",
    "MCPPendingActionPayloadDeletionEvidence",
    "MCPPendingActionPayloadError",
    "MCPPendingActionPayloadIdentity",
    "MCPPendingActionPayloadStore",
    "MCPValidatedPendingActionPayload",
    "PENDING_ACTION_PAYLOAD_ENCRYPTION_VERSION",
    "PENDING_ACTION_PAYLOAD_ORPHAN_GRACE",
    "pending_action_payload_ref",
]
