from __future__ import annotations

import os
import stat
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from src.core.contracts import MCPResultLifecycleStoragePort
from src.core.models import (
    MCPTerminalCandidateLifecycle,
    MCPTerminalCandidateLifecycleStatus,
)
from src.integrations.mcp.cp7_artifacts import (
    publish_or_compare_immutable,
    secure_read,
)


_MAX_ARTIFACT_BYTES = 64 * 1024


class MCPTerminalCandidateLifecycleError(RuntimeError):
    pass


class MCPTerminalCandidateLifecycleManager:
    """Moves consumed CP7 candidate triples under durable SQL markers."""

    def __init__(
        self,
        storage: MCPResultLifecycleStoragePort,
        active_root: str | os.PathLike[str],
        *,
        now_fn: Callable[[], datetime] | None = None,
        fault_hook: Callable[[str, MCPTerminalCandidateLifecycle], None] | None = None,
    ) -> None:
        self._storage = storage
        self._active_root = _validate_directory(Path(active_root), create=False)
        self._archive_root = _validate_directory(
            self._active_root.with_name(self._active_root.name + "-archive"),
            create=True,
        )
        self._now = now_fn or (
            lambda: datetime.now(timezone.utc).replace(tzinfo=None)
        )
        self._fault_hook = fault_hook

    @property
    def archive_root(self) -> Path:
        return self._archive_root

    async def repair_incomplete(self, *, limit: int = 1000) -> int:
        rows = await self._storage.list_incomplete_mcp_terminal_candidate_lifecycles(
            limit=limit
        )
        for row in rows:
            if row.status is MCPTerminalCandidateLifecycleStatus.ARCHIVING:
                await self._archive(row)
            elif row.status is MCPTerminalCandidateLifecycleStatus.DELETING:
                await self._delete(row)
            else:
                raise MCPTerminalCandidateLifecycleError(
                    "mcp_terminal_candidate_lifecycle_state_invalid"
                )
        return len(rows)

    async def run_once(self, *, limit: int = 1000) -> tuple[int, int, int]:
        repaired = await self.repair_incomplete(limit=limit)
        now = self._now()
        archiving = await self._storage.claim_mcp_terminal_candidate_archives(
            now, limit=limit
        )
        for row in archiving:
            await self._archive(row)
        deleting = await self._storage.claim_mcp_terminal_candidate_deletions(
            self._now(), limit=limit
        )
        for row in deleting:
            await self._delete(row)
        return repaired, len(archiving), len(deleting)

    async def _archive(self, row: MCPTerminalCandidateLifecycle) -> None:
        pairs = _artifact_pairs(row)
        for active_name, archive_name, expected_sha in pairs:
            source = self._active_root / active_name
            destination = self._archive_root / archive_name
            if source.exists() or source.is_symlink():
                artifact = secure_read(
                    source,
                    maximum_size=_MAX_ARTIFACT_BYTES,
                )
                if artifact.file_sha256 != expected_sha:
                    raise MCPTerminalCandidateLifecycleError(
                        "mcp_terminal_candidate_active_digest_conflict"
                    )
                published = publish_or_compare_immutable(
                    destination,
                    artifact.content,
                    maximum_size=_MAX_ARTIFACT_BYTES,
                )
                if published.artifact.file_sha256 != expected_sha:
                    raise MCPTerminalCandidateLifecycleError(
                        "mcp_terminal_candidate_archive_digest_conflict"
                    )
            else:
                artifact = secure_read(
                    destination,
                    maximum_size=_MAX_ARTIFACT_BYTES,
                )
                if artifact.file_sha256 != expected_sha:
                    raise MCPTerminalCandidateLifecycleError(
                        "mcp_terminal_candidate_archive_digest_conflict"
                    )
            self._inject("archive_copy", row)
        _fsync_directory(self._archive_root)
        for active_name, _archive_name, expected_sha in pairs:
            source = self._active_root / active_name
            if not source.exists() and not source.is_symlink():
                continue
            artifact = secure_read(source, maximum_size=_MAX_ARTIFACT_BYTES)
            if artifact.file_sha256 != expected_sha:
                raise MCPTerminalCandidateLifecycleError(
                    "mcp_terminal_candidate_active_digest_conflict"
                )
            source.unlink()
            self._inject("archive_unlink", row)
        _fsync_directory(self._active_root)
        saved = await self._storage.finish_mcp_terminal_candidate_archive(
            row.candidate_id,
            row.revision,
            self._now(),
        )
        if saved is None:
            raise MCPTerminalCandidateLifecycleError(
                "mcp_terminal_candidate_archive_cas_conflict"
            )

    async def _delete(self, row: MCPTerminalCandidateLifecycle) -> None:
        for _active_name, archive_name, expected_sha in _artifact_pairs(row):
            path = self._archive_root / archive_name
            if not path.exists() and not path.is_symlink():
                continue
            artifact = secure_read(path, maximum_size=_MAX_ARTIFACT_BYTES)
            if artifact.file_sha256 != expected_sha:
                raise MCPTerminalCandidateLifecycleError(
                    "mcp_terminal_candidate_archive_digest_conflict"
                )
            path.unlink()
            self._inject("delete_unlink", row)
        _fsync_directory(self._archive_root)
        saved = await self._storage.finish_mcp_terminal_candidate_deletion(
            row.candidate_id,
            row.revision,
            self._now(),
        )
        if saved is None:
            raise MCPTerminalCandidateLifecycleError(
                "mcp_terminal_candidate_delete_cas_conflict"
            )

    def _inject(self, point: str, row: MCPTerminalCandidateLifecycle) -> None:
        if self._fault_hook is not None:
            self._fault_hook(point, row)


def _artifact_pairs(
    row: MCPTerminalCandidateLifecycle,
) -> tuple[tuple[str, str, str], ...]:
    values = (
        (
            row.active_candidate_filename,
            row.archive_candidate_filename,
            row.candidate_file_sha256,
        ),
        (
            row.active_task_index_filename,
            row.archive_task_index_filename,
            row.task_index_file_sha256,
        ),
        (
            row.active_call_index_filename,
            row.archive_call_index_filename,
            row.call_index_file_sha256,
        ),
    )
    closed: list[tuple[str, str, str]] = []
    for active_name, archive_name, digest in values:
        if archive_name is None:
            raise MCPTerminalCandidateLifecycleError(
                "mcp_terminal_candidate_archive_binding_missing"
            )
        for name in (active_name, archive_name):
            if not name or Path(name).name != name or "/" in name or "\\" in name:
                raise MCPTerminalCandidateLifecycleError(
                    "mcp_terminal_candidate_filename_invalid"
                )
        if not digest.startswith("sha256:") or len(digest) != 71:
            raise MCPTerminalCandidateLifecycleError(
                "mcp_terminal_candidate_digest_invalid"
            )
        closed.append((active_name, archive_name, digest))
    return tuple(closed)


def _validate_directory(path: Path, *, create: bool) -> Path:
    if create:
        path.mkdir(mode=0o700, parents=False, exist_ok=True)
        os.chmod(path, 0o700, follow_symlinks=False)
    metadata = path.stat(follow_symlinks=False)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise MCPTerminalCandidateLifecycleError(
            "mcp_terminal_candidate_root_unsafe"
        )
    resolved = path.resolve(strict=True)
    resolved_metadata = resolved.stat(follow_symlinks=False)
    if (metadata.st_dev, metadata.st_ino) != (
        resolved_metadata.st_dev,
        resolved_metadata.st_ino,
    ):
        raise MCPTerminalCandidateLifecycleError(
            "mcp_terminal_candidate_root_identity_changed"
        )
    return resolved


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


__all__ = [
    "MCPTerminalCandidateLifecycleError",
    "MCPTerminalCandidateLifecycleManager",
]
