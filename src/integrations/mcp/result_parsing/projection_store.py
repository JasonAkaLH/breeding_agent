from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


MAX_PROJECTION_ENVELOPE_BYTES = 192 * 1024
MAX_PROJECTION_MANIFEST_BYTES = 16 * 1024
PROJECTION_SCHEMA = "maf.mcp.parsed_result_projection.v1"


class MCPProjectionStoreError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class MCPProjectionBinding:
    owner_user_id: str
    task_id: str
    node_id: str
    call_ref: str
    raw_sha256: str
    output_schema_sha256: str | None
    source: str
    parser_revision: str


@dataclass(frozen=True, slots=True)
class MCPProjectionStagingHandle:
    token: str
    path: str
    size_bytes: int
    projection_sha256: str
    device: int
    inode: int
    binding: MCPProjectionBinding


@dataclass(frozen=True, slots=True)
class MCPPublishedProjection:
    projection_ref: str
    projection_sha256: str


class MCPProjectionStore:
    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self._root, 0o700)
        _validate_directory(self._root)

    def stage(
        self, envelope: bytes, *, binding: MCPProjectionBinding
    ) -> MCPProjectionStagingHandle:
        data = bytes(envelope)
        _validate_envelope(data)
        token = secrets.token_urlsafe(24)
        path = self._root / f".staged-{token}.json"
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            metadata = os.stat(path, follow_symlinks=False)
            _validate_file(metadata, expected_size=len(data))
            _fsync_directory(self._root)
        except BaseException:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            raise
        return MCPProjectionStagingHandle(
            token=token,
            path=str(path),
            size_bytes=len(data),
            projection_sha256="sha256:" + hashlib.sha256(data).hexdigest(),
            device=metadata.st_dev,
            inode=metadata.st_ino,
            binding=binding,
        )

    def publish(self, handle: MCPProjectionStagingHandle) -> MCPPublishedProjection:
        staged_path = self._staged_path(handle)
        projection_ref = _projection_ref(handle)
        try:
            data = _read_bound_file(
                staged_path,
                expected_size=handle.size_bytes,
                expected_sha256=handle.projection_sha256,
                expected_device=handle.device,
                expected_inode=handle.inode,
            )
        except FileNotFoundError:
            self.load(
                projection_ref,
                binding=handle.binding,
                expected_projection_sha256=handle.projection_sha256,
            )
            return MCPPublishedProjection(projection_ref, handle.projection_sha256)
        data_path = self._root / f"{projection_ref}.json"
        manifest_path = self._root / f"{projection_ref}.manifest.json"
        _publish_or_compare(data_path, data)
        manifest = {
            "schema": "maf.mcp.parsed_result_projection_manifest.v1",
            "projection_ref": projection_ref,
            "owner_user_id": handle.binding.owner_user_id,
            "task_id": handle.binding.task_id,
            "node_id": handle.binding.node_id,
            "call_ref": handle.binding.call_ref,
            "raw_sha256": handle.binding.raw_sha256,
            "output_schema_sha256": handle.binding.output_schema_sha256,
            "source": handle.binding.source,
            "parser_revision": handle.binding.parser_revision,
            "projection_sha256": handle.projection_sha256,
            "size_bytes": handle.size_bytes,
        }
        manifest_bytes = json.dumps(
            manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        if len(manifest_bytes) > MAX_PROJECTION_MANIFEST_BYTES:
            raise MCPProjectionStoreError("projection manifest exceeds size limit")
        _publish_or_compare(manifest_path, manifest_bytes)
        staged_path.unlink()
        _fsync_directory(self._root)
        return MCPPublishedProjection(projection_ref, handle.projection_sha256)

    def discard(self, handle: MCPProjectionStagingHandle) -> None:
        path = self._staged_path(handle)
        try:
            _read_bound_file(
                path,
                expected_size=handle.size_bytes,
                expected_sha256=handle.projection_sha256,
                expected_device=handle.device,
                expected_inode=handle.inode,
            )
            path.unlink()
            _fsync_directory(self._root)
        except FileNotFoundError:
            return

    def load(
        self,
        projection_ref: str,
        *,
        binding: MCPProjectionBinding,
        expected_projection_sha256: str,
    ) -> Mapping[str, Any]:
        if not projection_ref.startswith("mcp-projection-") or len(projection_ref) != 79:
            raise MCPProjectionStoreError("projection reference is invalid")
        data_path = self._root / f"{projection_ref}.json"
        manifest_path = self._root / f"{projection_ref}.manifest.json"
        manifest_bytes = _read_private_file(manifest_path, MAX_PROJECTION_MANIFEST_BYTES)
        try:
            manifest = json.loads(manifest_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MCPProjectionStoreError("projection manifest is invalid") from exc
        if not isinstance(manifest, dict):
            raise MCPProjectionStoreError("projection manifest is invalid")
        expected_manifest = {
            "schema": "maf.mcp.parsed_result_projection_manifest.v1",
            "projection_ref": projection_ref,
            "owner_user_id": binding.owner_user_id,
            "task_id": binding.task_id,
            "node_id": binding.node_id,
            "call_ref": binding.call_ref,
            "raw_sha256": binding.raw_sha256,
            "output_schema_sha256": binding.output_schema_sha256,
            "source": binding.source,
            "parser_revision": binding.parser_revision,
            "projection_sha256": expected_projection_sha256,
            "size_bytes": manifest.get("size_bytes"),
        }
        if manifest != expected_manifest or not isinstance(manifest["size_bytes"], int):
            raise MCPProjectionStoreError("projection manifest authority does not match")
        data = _read_private_file(data_path, MAX_PROJECTION_ENVELOPE_BYTES)
        if len(data) != manifest["size_bytes"] or (
            "sha256:" + hashlib.sha256(data).hexdigest() != expected_projection_sha256
        ):
            raise MCPProjectionStoreError("projection content identity does not match")
        _validate_envelope(data)
        return json.loads(data)

    def cleanup_staged(self, *, older_than_seconds: float = 24 * 60 * 60) -> int:
        cutoff = time.time() - older_than_seconds
        removed = 0
        for path in self._root.glob(".staged-*.json"):
            metadata = os.stat(path, follow_symlinks=False)
            _validate_file(metadata, expected_size=metadata.st_size)
            if metadata.st_mtime > cutoff:
                continue
            path.unlink()
            removed += 1
        if removed:
            _fsync_directory(self._root)
        return removed

    def _staged_path(self, handle: MCPProjectionStagingHandle) -> Path:
        path = Path(handle.path)
        if path.parent != self._root or path.name != f".staged-{handle.token}.json":
            raise MCPProjectionStoreError("projection staging handle path is invalid")
        return path


def _validate_envelope(data: bytes) -> None:
    if len(data) > MAX_PROJECTION_ENVELOPE_BYTES:
        raise MCPProjectionStoreError("projection envelope exceeds size limit")
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MCPProjectionStoreError("projection envelope is invalid") from exc
    if not isinstance(value, dict) or value.get("schema") != PROJECTION_SCHEMA:
        raise MCPProjectionStoreError("projection envelope schema is invalid")


def _projection_ref(handle: MCPProjectionStagingHandle) -> str:
    identity_bytes = json.dumps(
        {
            "owner_user_id": handle.binding.owner_user_id,
            "task_id": handle.binding.task_id,
            "node_id": handle.binding.node_id,
            "call_ref": handle.binding.call_ref,
            "raw_sha256": handle.binding.raw_sha256,
            "output_schema_sha256": handle.binding.output_schema_sha256,
            "source": handle.binding.source,
            "parser_revision": handle.binding.parser_revision,
            "projection_sha256": handle.projection_sha256,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "mcp-projection-" + hashlib.sha256(identity_bytes).hexdigest()


def _publish_or_compare(path: Path, data: bytes) -> None:
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except FileExistsError:
        if _read_private_file(path, max(len(data), MAX_PROJECTION_MANIFEST_BYTES)) != data:
            raise MCPProjectionStoreError("projection content-addressed publication conflicts")
        return
    with os.fdopen(descriptor, "wb", closefd=True) as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    _fsync_directory(path.parent)


def _read_bound_file(
    path: Path,
    *,
    expected_size: int,
    expected_sha256: str,
    expected_device: int,
    expected_inode: int,
) -> bytes:
    before = os.stat(path, follow_symlinks=False)
    _validate_file(before, expected_size=expected_size)
    if before.st_dev != expected_device or before.st_ino != expected_inode:
        raise MCPProjectionStoreError("projection staging identity changed")
    data = _read_private_file(path, MAX_PROJECTION_ENVELOPE_BYTES)
    if "sha256:" + hashlib.sha256(data).hexdigest() != expected_sha256:
        raise MCPProjectionStoreError("projection staging digest changed")
    return data


def _read_private_file(path: Path, maximum_size: int) -> bytes:
    before = os.stat(path, follow_symlinks=False)
    _validate_file(before, expected_size=before.st_size)
    if before.st_size > maximum_size:
        raise MCPProjectionStoreError("private projection file exceeds size limit")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb", closefd=True) as handle:
        opened = os.fstat(handle.fileno())
        if opened.st_dev != before.st_dev or opened.st_ino != before.st_ino:
            raise MCPProjectionStoreError("private projection file identity changed")
        data = handle.read(maximum_size + 1)
    if len(data) != before.st_size:
        raise MCPProjectionStoreError("private projection file size changed")
    return data


def _validate_directory(path: Path) -> None:
    metadata = os.stat(path, follow_symlinks=False)
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o700:
        raise MCPProjectionStoreError("projection directory is unsafe")


def _validate_file(metadata: os.stat_result, *, expected_size: int) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
        or metadata.st_size != expected_size
    ):
        raise MCPProjectionStoreError("projection file is unsafe")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
