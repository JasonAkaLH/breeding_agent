from __future__ import annotations

import asyncio
import contextlib
import errno
import hashlib
import os
import secrets
import shutil
import time
from collections.abc import AsyncIterator, Callable, Collection
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from .client import MCPClientError, MCPProtocolError


class MCPTemporaryResultError(MCPClientError):
    pass


class MCPCapacityUnavailableError(MCPTemporaryResultError):
    def __init__(self) -> None:
        super().__init__(
            "User MCP runtime capacity is temporarily unavailable.",
            code="mcp_capacity_unavailable",
            retriable=True,
        )


class MCPTemporaryStorageExhaustedError(MCPTemporaryResultError):
    def __init__(self) -> None:
        super().__init__(
            "User MCP temporary storage is exhausted.",
            code="temporary_storage_exhausted",
            retriable=True,
        )


@dataclass(frozen=True, slots=True)
class MCPTemporaryResultRef:
    """Opaque completed-result descriptor; it deliberately contains no path."""

    ref: str
    size_bytes: int
    sha256: str
    storage: str

    def as_payload(self) -> dict[str, object]:
        return {
            "ref": self.ref,
            "sizeBytes": self.size_bytes,
            "sha256": self.sha256,
            "storage": self.storage,
        }


@runtime_checkable
class MCPResultSink(Protocol):
    async def write(self, chunk: bytes) -> None: ...

    async def materialize(self, *, max_bytes: int) -> bytes: ...

    async def finalize(self) -> MCPTemporaryResultRef: ...

    async def abort(self) -> None: ...


@dataclass(frozen=True, slots=True)
class MCPTemporaryResultCapacityConfig:
    max_active_user_mcp_calls_per_instance: int
    temporary_disk_low_watermark_bytes: int

    def __post_init__(self) -> None:
        for field_name in (
            "max_active_user_mcp_calls_per_instance",
            "temporary_disk_low_watermark_bytes",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{field_name} must be a positive integer.")


class MCPTemporaryResultCapacity:
    """Fail-fast admission guard used before a remote tools/call is sent."""

    def __init__(
        self,
        config: MCPTemporaryResultCapacityConfig,
        *,
        storage_root: Path,
        free_bytes: Callable[[Path], int] | None = None,
    ) -> None:
        self._config = config
        self._storage_root = storage_root
        self._free_bytes = free_bytes or _disk_free_bytes
        self._active = 0
        self._lock = asyncio.Lock()

    @property
    def active_calls(self) -> int:
        return self._active

    @asynccontextmanager
    async def admit(self) -> AsyncIterator[None]:
        async with self._lock:
            if self._active >= self._config.max_active_user_mcp_calls_per_instance:
                raise MCPCapacityUnavailableError()
            if self._free_bytes(self._storage_root) < self._config.temporary_disk_low_watermark_bytes:
                raise MCPCapacityUnavailableError()
            self._active += 1
        try:
            yield
        finally:
            async with self._lock:
                self._active -= 1


@dataclass(slots=True)
class _StoredResult:
    task_key: str
    scope_id: str | None
    memory: bytes | None
    path: Path | None
    promoted: bool = False


class MCPTemporaryResultStore:
    """Task-scoped memory-to-file spool with opaque references."""

    def __init__(self, root: Path, *, memory_threshold_bytes: int) -> None:
        if isinstance(memory_threshold_bytes, bool) or not isinstance(memory_threshold_bytes, int) or memory_threshold_bytes < 0:
            raise ValueError("memory_threshold_bytes must be a non-negative integer.")
        self._root = Path(root)
        self._memory_threshold_bytes = memory_threshold_bytes
        self._tasks: dict[str, str] = {}
        self._results: dict[str, _StoredResult] = {}
        self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self._root, 0o700)

    @property
    def root(self) -> Path:
        return self._root

    def create_sink(self, task_id: str, *, scope_id: str | None = None) -> MCPResultSink:
        task_key = self._task_key(task_id)
        return _MemoryToFileSink(
            store=self,
            task_key=task_key,
            scope_id=str(scope_id) if scope_id is not None else None,
            threshold=self._memory_threshold_bytes,
        )

    async def iter_bytes(self, result_ref: MCPTemporaryResultRef, *, chunk_size: int = 64 * 1024) -> AsyncIterator[bytes]:
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive.")
        stored = self._results.get(result_ref.ref)
        if stored is None:
            raise KeyError("Unknown or expired MCP temporary result reference.")
        if stored.memory is not None:
            for offset in range(0, len(stored.memory), chunk_size):
                yield stored.memory[offset : offset + chunk_size]
            return
        if stored.path is None:
            raise KeyError("Unknown or expired MCP temporary result reference.")
        handle = await asyncio.to_thread(open, stored.path, "rb")
        try:
            while chunk := await asyncio.to_thread(handle.read, chunk_size):
                yield chunk
        finally:
            await asyncio.to_thread(handle.close)

    def mark_promoted(self, result_ref: MCPTemporaryResultRef) -> None:
        stored = self._results.get(result_ref.ref)
        if stored is None:
            raise KeyError("Unknown or expired MCP temporary result reference.")
        stored.promoted = True

    async def discard(self, result_ref: MCPTemporaryResultRef) -> None:
        stored = self._results.pop(result_ref.ref, None)
        if stored is None or stored.promoted:
            return
        if stored.path is not None:
            await _unlink(stored.path)

    async def cleanup_scope(self, scope_id: str) -> None:
        normalized = str(scope_id)
        for ref, stored in list(self._results.items()):
            if stored.scope_id == normalized and not stored.promoted:
                self._results.pop(ref, None)
                if stored.path is not None:
                    await _unlink(stored.path)

    async def cleanup_task(self, task_id: str) -> None:
        task_key = self._tasks.pop(str(task_id), None)
        if task_key is None:
            return
        for ref, stored in list(self._results.items()):
            if stored.task_key == task_key and not stored.promoted:
                self._results.pop(ref, None)
                if stored.path is not None:
                    await _unlink(stored.path)
        task_dir = self._root / task_key
        if task_dir.exists() and not any(stored.task_key == task_key for stored in self._results.values()):
            await asyncio.to_thread(shutil.rmtree, task_dir, True)

    def active_task_keys(self) -> frozenset[str]:
        return frozenset(self._tasks.values())

    def _task_key(self, task_id: str) -> str:
        normalized = str(task_id)
        if normalized in self._tasks:
            return self._tasks[normalized]
        task_key = f"task-{secrets.token_urlsafe(18)}"
        task_dir = self._root / task_key
        task_dir.mkdir(mode=0o700)
        os.chmod(task_dir, 0o700)
        self._tasks[normalized] = task_key
        return task_key

    def _register(self, result: MCPTemporaryResultRef, stored: _StoredResult) -> None:
        self._results[result.ref] = stored

    async def _remove_empty_task_dir(self, task_key: str) -> None:
        task_dir = self._root / task_key
        if task_dir.exists() and not any(stored.task_key == task_key for stored in self._results.values()):
            await asyncio.to_thread(shutil.rmtree, task_dir, True)


class _MemoryToFileSink:
    def __init__(self, *, store: MCPTemporaryResultStore, task_key: str, scope_id: str | None, threshold: int) -> None:
        self._store = store
        self._task_key = task_key
        self._scope_id = scope_id
        self._threshold = threshold
        self._memory = bytearray()
        self._path: Path | None = None
        self._handle = None
        self._file_buffer = bytearray()
        self._digest = hashlib.sha256()
        self._size = 0
        self._finished = False

    async def write(self, chunk: bytes) -> None:
        if self._finished:
            raise RuntimeError("MCP result sink is already finalized or aborted.")
        data = bytes(chunk)
        if not data:
            return
        try:
            if self._handle is None and self._size + len(data) > self._threshold:
                await self._spill()
            if self._handle is None:
                self._memory.extend(data)
            else:
                self._file_buffer.extend(data)
                if len(self._file_buffer) >= 64 * 1024:
                    await self._flush_file_buffer()
            self._digest.update(data)
            self._size += len(data)
        except OSError as exc:
            await self.abort()
            if exc.errno in {errno.ENOSPC, errno.EDQUOT}:
                raise MCPTemporaryStorageExhaustedError() from exc
            raise

    async def finalize(self) -> MCPTemporaryResultRef:
        if self._finished:
            raise RuntimeError("MCP result sink is already finalized or aborted.")
        try:
            if self._handle is not None:
                await self._flush_file_buffer()
                await asyncio.to_thread(self._handle.flush)
                await asyncio.to_thread(self._handle.close)
                self._handle = None
        except OSError as exc:
            await self.abort()
            if exc.errno in {errno.ENOSPC, errno.EDQUOT}:
                raise MCPTemporaryStorageExhaustedError() from exc
            raise
        self._finished = True
        result = MCPTemporaryResultRef(
            ref=f"mcp-result-{secrets.token_urlsafe(24)}",
            size_bytes=self._size,
            sha256=self._digest.hexdigest(),
            storage="file" if self._path is not None else "memory",
        )
        self._store._register(
            result,
            _StoredResult(
                task_key=self._task_key,
                scope_id=self._scope_id,
                memory=None if self._path is not None else bytes(self._memory),
                path=self._path,
            ),
        )
        self._memory.clear()
        self._file_buffer.clear()
        return result

    async def materialize(self, *, max_bytes: int) -> bytes:
        if self._finished:
            raise RuntimeError("MCP result sink is already finalized or aborted.")
        if max_bytes <= 0 or self._size > max_bytes:
            raise MCPProtocolError(
                "MCP streaming control result exceeded metadata limit."
            )
        if self._handle is None:
            return bytes(self._memory)
        await self._flush_file_buffer()
        await asyncio.to_thread(self._handle.flush)
        assert self._path is not None
        return await asyncio.to_thread(self._path.read_bytes)

    async def abort(self) -> None:
        if self._finished:
            return
        self._finished = True
        self._memory.clear()
        self._file_buffer.clear()
        if self._handle is not None:
            with contextlib.suppress(OSError):
                await asyncio.to_thread(self._handle.close)
            self._handle = None
        if self._path is not None:
            await _unlink(self._path)

    async def _spill(self) -> None:
        task_dir = self._store.root / self._task_key
        path = task_dir / f"result-{secrets.token_urlsafe(18)}.json"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.chmod(path, 0o600)
        self._path = path
        self._handle = os.fdopen(descriptor, "wb")
        if self._memory:
            self._file_buffer.extend(self._memory)
            self._memory.clear()

    async def _flush_file_buffer(self) -> None:
        if self._handle is not None and self._file_buffer:
            data = bytes(self._file_buffer)
            self._file_buffer.clear()
            await asyncio.to_thread(self._handle.write, data)


class MCPTemporaryResultJanitor:
    def __init__(self, root: Path, *, safe_age_seconds: float, clock: Callable[[], float] = time.time) -> None:
        if safe_age_seconds < 0:
            raise ValueError("safe_age_seconds must be non-negative.")
        self._root = Path(root)
        self._safe_age_seconds = safe_age_seconds
        self._clock = clock

    async def cleanup_orphans(self, *, active_task_keys: Collection[str] = ()) -> tuple[str, ...]:
        if not self._root.exists():
            return ()
        active = frozenset(active_task_keys)
        cutoff = self._clock() - self._safe_age_seconds
        removed: list[str] = []
        for candidate in self._root.iterdir():
            if not candidate.is_dir() or not candidate.name.startswith("task-") or candidate.name in active:
                continue
            try:
                modified = candidate.stat().st_mtime
            except FileNotFoundError:
                continue
            if modified > cutoff:
                continue
            await asyncio.to_thread(shutil.rmtree, candidate, True)
            removed.append(candidate.name)
        return tuple(sorted(removed))


def _disk_free_bytes(path: Path) -> int:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return shutil.disk_usage(path).free


async def _unlink(path: Path) -> None:
    try:
        await asyncio.to_thread(path.unlink)
    except FileNotFoundError:
        pass


__all__ = [
    "MCPCapacityUnavailableError",
    "MCPResultSink",
    "MCPTemporaryResultCapacity",
    "MCPTemporaryResultCapacityConfig",
    "MCPTemporaryResultError",
    "MCPTemporaryResultJanitor",
    "MCPTemporaryResultRef",
    "MCPTemporaryResultStore",
    "MCPTemporaryStorageExhaustedError",
]
