from __future__ import annotations

import hashlib
import hmac
import os
import re
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


PROJECT_SKILL_BUNDLE_DIGEST_ENV = "MAF_PROJECT_SKILL_BUNDLE_DIGEST"
PROJECT_SKILL_BUNDLE_MAX_FILES = 1_000
PROJECT_SKILL_BUNDLE_MAX_BYTES = 256 * 1024 * 1024
PROJECT_SKILL_BUNDLE_DEADLINE_SECONDS = 2.0
_DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
_IGNORED_DIRECTORY_NAMES = frozenset({".git", "__pycache__"})
_READ_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class ProjectSkillBundleDigest:
    digest: str
    file_count: int
    total_bytes: int
    duration_ms: int


class ProjectSkillBundleDigestError(ValueError):
    def __init__(self, code: str, reason: str) -> None:
        super().__init__(code)
        self.code = code
        self.reason = reason


@dataclass(frozen=True, slots=True)
class _BundleFile:
    path: Path
    relative_bytes: bytes
    device: int
    inode: int
    size: int


def compute_project_skill_bundle_digest(
    root: str | Path,
    *,
    max_files: int = PROJECT_SKILL_BUNDLE_MAX_FILES,
    max_total_bytes: int = PROJECT_SKILL_BUNDLE_MAX_BYTES,
    deadline_seconds: float = PROJECT_SKILL_BUNDLE_DEADLINE_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
) -> ProjectSkillBundleDigest:
    if max_files < 0 or max_total_bytes < 0 or deadline_seconds <= 0:
        raise ValueError("Project Skill bundle limits must be positive")
    started_at = monotonic()
    root_path = Path(root).expanduser()
    try:
        resolved_root = root_path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ProjectSkillBundleDigestError(
            "project_skill_bundle_unsafe_entry", "root_missing"
        ) from exc
    if not resolved_root.is_dir():
        raise ProjectSkillBundleDigestError(
            "project_skill_bundle_unsafe_entry", "root_not_directory"
        )

    files: list[_BundleFile] = []
    total_bytes = _collect_bundle_files(
        resolved_root,
        resolved_root,
        files,
        max_files=max_files,
        max_total_bytes=max_total_bytes,
        started_at=started_at,
        deadline_seconds=deadline_seconds,
        monotonic=monotonic,
    )
    files.sort(key=lambda item: item.relative_bytes)
    bundle_hash = hashlib.sha256()
    for item in files:
        _check_deadline(started_at, deadline_seconds, monotonic)
        file_hash = _hash_bundle_file(
            item,
            started_at=started_at,
            deadline_seconds=deadline_seconds,
            monotonic=monotonic,
        )
        bundle_hash.update(item.relative_bytes)
        bundle_hash.update(b"\0")
        bundle_hash.update(str(item.size).encode("ascii"))
        bundle_hash.update(b"\0")
        bundle_hash.update(file_hash.encode("ascii"))
        bundle_hash.update(b"\n")
    finished_at = monotonic()
    _check_deadline(started_at, deadline_seconds, lambda: finished_at)
    return ProjectSkillBundleDigest(
        digest="sha256:" + bundle_hash.hexdigest(),
        file_count=len(files),
        total_bytes=total_bytes,
        duration_ms=max(0, int((finished_at - started_at) * 1000)),
    )


def validate_project_skill_bundle_digest(
    root: str | Path,
    expected_digest: str,
    *,
    max_files: int = PROJECT_SKILL_BUNDLE_MAX_FILES,
    max_total_bytes: int = PROJECT_SKILL_BUNDLE_MAX_BYTES,
    deadline_seconds: float = PROJECT_SKILL_BUNDLE_DEADLINE_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
) -> ProjectSkillBundleDigest:
    normalized = str(expected_digest).strip()
    if _DIGEST_PATTERN.fullmatch(normalized) is None:
        raise ProjectSkillBundleDigestError(
            "project_skill_bundle_digest_invalid", "format"
        )
    result = compute_project_skill_bundle_digest(
        root,
        max_files=max_files,
        max_total_bytes=max_total_bytes,
        deadline_seconds=deadline_seconds,
        monotonic=monotonic,
    )
    if not hmac.compare_digest(result.digest, normalized):
        raise ProjectSkillBundleDigestError(
            "project_skill_bundle_digest_mismatch", "mismatch"
        )
    return result


def _collect_bundle_files(
    root: Path,
    directory: Path,
    files: list[_BundleFile],
    *,
    max_files: int,
    max_total_bytes: int,
    started_at: float,
    deadline_seconds: float,
    monotonic: Callable[[], float],
) -> int:
    total_bytes = sum(item.size for item in files)
    _check_deadline(started_at, deadline_seconds, monotonic)
    try:
        entries = list(os.scandir(directory))
    except OSError as exc:
        raise ProjectSkillBundleDigestError(
            "project_skill_bundle_unsafe_entry", "unreadable_directory"
        ) from exc
    for entry in entries:
        _check_deadline(started_at, deadline_seconds, monotonic)
        if entry.name in _IGNORED_DIRECTORY_NAMES:
            continue
        path = Path(entry.path)
        relative = path.relative_to(root).as_posix()
        relative_bytes = _encode_relative_path(relative)
        try:
            metadata = entry.stat(follow_symlinks=False)
        except OSError as exc:
            raise ProjectSkillBundleDigestError(
                "project_skill_bundle_unsafe_entry", "unreadable_entry"
            ) from exc
        mode = metadata.st_mode
        if stat.S_ISLNK(mode):
            raise ProjectSkillBundleDigestError(
                "project_skill_bundle_unsafe_entry", "symlink"
            )
        if stat.S_ISDIR(mode):
            total_bytes = _collect_bundle_files(
                root,
                path,
                files,
                max_files=max_files,
                max_total_bytes=max_total_bytes,
                started_at=started_at,
                deadline_seconds=deadline_seconds,
                monotonic=monotonic,
            )
            continue
        if not stat.S_ISREG(mode):
            raise ProjectSkillBundleDigestError(
                "project_skill_bundle_unsafe_entry", "special_file"
            )
        if path.suffix == ".pyc":
            continue
        if len(files) >= max_files:
            raise ProjectSkillBundleDigestError(
                "project_skill_bundle_unsafe_entry", "file_limit"
            )
        total_bytes += int(metadata.st_size)
        if total_bytes > max_total_bytes:
            raise ProjectSkillBundleDigestError(
                "project_skill_bundle_unsafe_entry", "byte_limit"
            )
        files.append(
            _BundleFile(
                path=path,
                relative_bytes=relative_bytes,
                device=int(metadata.st_dev),
                inode=int(metadata.st_ino),
                size=int(metadata.st_size),
            )
        )
    return total_bytes


def _hash_bundle_file(
    item: _BundleFile,
    *,
    started_at: float,
    deadline_seconds: float,
    monotonic: Callable[[], float],
) -> str:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(item.path, flags)
    except OSError as exc:
        raise ProjectSkillBundleDigestError(
            "project_skill_bundle_unsafe_entry", "unreadable_file"
        ) from exc
    file_hash = hashlib.sha256()
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or int(opened.st_dev) != item.device
            or int(opened.st_ino) != item.inode
            or int(opened.st_size) != item.size
        ):
            raise ProjectSkillBundleDigestError(
                "project_skill_bundle_unsafe_entry", "changed_during_read"
            )
        while True:
            _check_deadline(started_at, deadline_seconds, monotonic)
            chunk = os.read(descriptor, _READ_CHUNK_BYTES)
            if not chunk:
                break
            file_hash.update(chunk)
        closed = os.fstat(descriptor)
        if int(closed.st_size) != item.size:
            raise ProjectSkillBundleDigestError(
                "project_skill_bundle_unsafe_entry", "changed_during_read"
            )
    finally:
        os.close(descriptor)
    return file_hash.hexdigest()


def _check_deadline(
    started_at: float,
    deadline_seconds: float,
    monotonic: Callable[[], float],
) -> None:
    if monotonic() - started_at > deadline_seconds:
        raise ProjectSkillBundleDigestError(
            "project_skill_bundle_unsafe_entry", "deadline"
        )


def _encode_relative_path(relative: str) -> bytes:
    try:
        return relative.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ProjectSkillBundleDigestError(
            "project_skill_bundle_unsafe_entry", "non_utf8_path"
        ) from exc
