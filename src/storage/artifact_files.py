from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from src.integrations.rust_safety_contract import normalize_storage_key

from .path_safety import sanitize_download_filename


@dataclass(slots=True, frozen=True)
class StoredArtifactFile:
    storage_key: str
    filename: str
    size_bytes: int
    sha256: str


class LocalArtifactFileStore:
    """Local managed file store for downloadable artifact bodies.

    The public database stores only opaque storage keys produced by this class;
    callers must not pass real filesystem paths to API responses or prompts.
    """

    def __init__(self, root_dir: str | Path) -> None:
        self._root_dir = Path(root_dir)
        self._root_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self._root_dir, 0o700, follow_symlinks=False)
        _validate_private_directory(self._root_dir)

    @property
    def root_dir(self) -> Path:
        return self._root_dir

    def save_file(self, *, artifact_id: str, filename: str, source_path: str | Path) -> StoredArtifactFile:
        source = Path(source_path)
        if not source.exists() or not source.is_file():
            raise FileNotFoundError(f"Artifact source file does not exist: {source}")
        safe_filename = sanitize_download_filename(filename)
        storage_key = normalize_storage_key(f"{sanitize_storage_component(artifact_id)}/{safe_filename}")
        artifact_dir = self._safe_artifact_dir(artifact_id)
        target = artifact_dir / safe_filename
        if artifact_dir.exists():
            return self._compare_existing_file(
                source=source,
                target=target,
                storage_key=storage_key,
                filename=safe_filename,
            )
        artifact_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
        os.chmod(artifact_dir, 0o700, follow_symlinks=False)
        digest = hashlib.sha256()
        size = 0
        try:
            with source.open("rb") as src:
                descriptor = os.open(
                    target,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                with os.fdopen(descriptor, "wb") as dst:
                    while True:
                        chunk = src.read(1024 * 1024)
                        if not chunk:
                            break
                        digest.update(chunk)
                        size += len(chunk)
                        dst.write(chunk)
                    dst.flush()
                    os.fsync(dst.fileno())
            os.chmod(target, 0o600, follow_symlinks=False)
            _fsync_directory(artifact_dir)
            _fsync_directory(self._root_dir)
        except Exception:
            shutil.rmtree(artifact_dir, ignore_errors=True)
            raise
        return StoredArtifactFile(
            storage_key=storage_key,
            filename=safe_filename,
            size_bytes=size,
            sha256=digest.hexdigest(),
        )

    @staticmethod
    def _compare_existing_file(
        *,
        source: Path,
        target: Path,
        storage_key: str,
        filename: str,
    ) -> StoredArtifactFile:
        if not target.exists() or target.is_symlink():
            raise FileExistsError("Artifact destination conflicts with existing state")
        _validate_private_directory(target.parent)
        siblings = tuple(target.parent.iterdir())
        metadata = target.stat(follow_symlinks=False)
        if (
            siblings != (target,)
            or not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
        ):
            raise FileExistsError("Artifact destination conflicts with existing state")
        source_size, source_sha256 = _hash_file(source)
        target_size, target_sha256 = _hash_private_file(target)
        if (source_size, source_sha256) != (target_size, target_sha256):
            raise FileExistsError("Artifact destination conflicts with existing content")
        return StoredArtifactFile(
            storage_key=storage_key,
            filename=filename,
            size_bytes=target_size,
            sha256=target_sha256,
        )

    def open_path(self, storage_key: str) -> Path:
        return self._resolve_storage_key(storage_key)

    def read_utf8(
        self,
        storage_key: str,
        *,
        expected_size_bytes: int,
        expected_sha256: str,
    ) -> str:
        if (
            isinstance(expected_size_bytes, bool)
            or not isinstance(expected_size_bytes, int)
            or expected_size_bytes < 0
        ):
            raise ValueError("Artifact expected size is invalid")
        expected_digest = str(expected_sha256).removeprefix("sha256:")
        if re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None:
            raise ValueError("Artifact expected digest is invalid")
        path = self._resolve_storage_key(storage_key)
        _validate_private_directory(path.parent)
        before = path.stat(follow_symlinks=False)
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        digest = hashlib.sha256()
        body = bytearray()
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if opened.st_size != expected_size_bytes:
                raise ValueError("Artifact content identity changed")
            while chunk := handle.read(1024 * 1024):
                body.extend(chunk)
                digest.update(chunk)
            after = os.fstat(handle.fileno())
        after_path = path.stat(follow_symlinks=False)
        identities = {
            (
                item.st_dev,
                item.st_ino,
                item.st_mode,
                item.st_uid,
                item.st_nlink,
                item.st_size,
            )
            for item in (before, opened, after, after_path)
        }
        if (
            len(identities) != 1
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.getuid()
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_nlink != 1
            or len(body) != expected_size_bytes
            or digest.hexdigest() != expected_digest
        ):
            raise ValueError("Artifact content identity changed")
        try:
            return body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("Artifact content is not UTF-8 text") from exc

    def delete(self, storage_key: str) -> bool:
        path = self._resolve_storage_key(storage_key)
        existed = path.exists()
        if existed:
            path.unlink()
        parent = path.parent
        if parent != self._root_dir and parent.exists():
            try:
                parent.rmdir()
            except OSError:
                pass
        return existed

    def _safe_artifact_dir(self, artifact_id: str) -> Path:
        normalized = sanitize_storage_component(artifact_id)
        path = (self._root_dir / normalized).resolve()
        if not path.is_relative_to(self._root_dir.resolve()):
            raise ValueError("Artifact id escapes artifact store")
        return path

    def _resolve_storage_key(self, storage_key: str) -> Path:
        storage_key = normalize_storage_key(storage_key)
        pure = PurePosixPath(storage_key)
        if pure.is_absolute() or len(pure.parts) != 2 or any(part in {"", ".", ".."} for part in pure.parts):
            raise ValueError("Invalid artifact storage key")
        artifact_id, filename = pure.parts
        path = (self._root_dir / sanitize_storage_component(artifact_id) / sanitize_download_filename(filename)).resolve()
        if not path.is_relative_to(self._root_dir.resolve()):
            raise ValueError("Artifact storage key escapes artifact store")
        return path


def sanitize_storage_component(value: str) -> str:
    text = str(value).strip()
    text = re.sub(r"[^A-Za-z0-9_.:-]+", "_", text)
    text = text.strip("._")
    if not text:
        raise ValueError("Storage component cannot be empty")
    return text[:160]


def build_file_storage_ref(metadata: Mapping[str, Any]) -> str:
    return json.dumps(dict(metadata), ensure_ascii=False, sort_keys=True, default=str)


def parse_file_storage_ref(storage_ref: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(storage_ref)
    except (TypeError, json.JSONDecodeError):
        return None
    return dict(parsed) if isinstance(parsed, Mapping) else None


def is_active_skill_output_file(metadata: Mapping[str, Any] | None) -> bool:
    return bool(
        metadata
        and metadata.get("source_kind") == "skill_output"
        and metadata.get("retention_status") == "active"
        and isinstance(metadata.get("storage_key"), str)
    )


def is_active_managed_output_file(metadata: Mapping[str, Any] | None) -> bool:
    return bool(
        metadata
        and metadata.get("source_kind") in {"skill_output", "mcp_result"}
        and metadata.get("retention_status") == "active"
        and isinstance(metadata.get("storage_key"), str)
    )


def _hash_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _hash_private_file(path: Path) -> tuple[int, str]:
    before = path.stat(follow_symlinks=False)
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
    )
    digest = hashlib.sha256()
    size = 0
    with os.fdopen(descriptor, "rb") as handle:
        opened = os.fstat(handle.fileno())
        while chunk := handle.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
        after = os.fstat(handle.fileno())
    after_path = path.stat(follow_symlinks=False)
    identities = {
        (
            item.st_dev,
            item.st_ino,
            item.st_mode,
            item.st_uid,
            item.st_nlink,
            item.st_size,
        )
        for item in (before, opened, after, after_path)
    }
    if (
        len(identities) != 1
        or not stat.S_ISREG(opened.st_mode)
        or opened.st_uid != os.getuid()
        or stat.S_IMODE(opened.st_mode) != 0o600
        or opened.st_nlink != 1
    ):
        raise FileExistsError("Artifact destination identity changed")
    return size, digest.hexdigest()


def _validate_private_directory(path: Path) -> None:
    metadata = path.stat(follow_symlinks=False)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise ValueError("Artifact directory is not private")
    resolved = path.resolve(strict=True)
    resolved_metadata = resolved.stat(follow_symlinks=False)
    if (metadata.st_dev, metadata.st_ino) != (
        resolved_metadata.st_dev,
        resolved_metadata.st_ino,
    ):
        raise ValueError("Artifact directory identity changed")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
