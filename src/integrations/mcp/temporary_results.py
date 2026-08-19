from __future__ import annotations

import asyncio
import contextlib
import errno
import hashlib
import json
import os
import secrets
import shutil
import stat
import threading
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Collection
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from src.core.models import (
    MCPCallRecord,
    MCPDurableResultLifecycle,
    MCPDurableResultSnapshot,
    MCPTerminalResultReceipt,
)

from .client import MCPClientError, MCPProtocolError


MAX_DURABLE_MCP_RESULT_BYTES = 64 * 1024 * 1024


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


class MCPResultTooLargeError(MCPTemporaryResultError):
    def __init__(self) -> None:
        super().__init__(
            "User MCP result exceeded the durable result limit.",
            code="mcp_result_too_large",
            retriable=False,
        )


class MCPAdmissionCancelledError(MCPTemporaryResultError):
    def __init__(self) -> None:
        super().__init__(
            "User MCP admission was cancelled.",
            code="mcp_admission_cancelled",
            retriable=False,
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


@dataclass(frozen=True, slots=True)
class MCPDurableResultIdentity:
    result_ref: str
    owner_user_id: str | None
    task_id: str
    node_id: str | None
    call_id: str | None
    size_bytes: int
    content_sha256: str
    store_kind: str = "durable_content_addressed"


@dataclass(slots=True)
class _HeldDurableResult:
    snapshot: MCPDurableResultSnapshot
    data_descriptor: int
    manifest_descriptor: int
    data_path: Path
    manifest_path: Path
    data_identity: tuple[int, ...]
    manifest_identity: tuple[int, ...]


class MCPDurableResultSnapshotAuthority:
    def __init__(self, store: "MCPTemporaryResultStore") -> None:
        self._store = store
        self._held: dict[str, list[_HeldDurableResult]] = {}
        self._lock = threading.Lock()

    @asynccontextmanager
    async def open_snapshot(
        self,
        *,
        result_ref: str,
        owner_user_id: str,
        task_id: str,
        node_id: str,
        call_id: str,
        expected_size_bytes: int,
        expected_content_sha256: str,
        expected_store_kind: str,
    ) -> AsyncIterator[MCPDurableResultSnapshot]:
        held = await asyncio.to_thread(
            self._open_and_register,
            result_ref=str(result_ref),
            owner_user_id=str(owner_user_id),
            task_id=str(task_id),
            node_id=str(node_id),
            call_id=str(call_id),
            expected_size_bytes=expected_size_bytes,
            expected_content_sha256=expected_content_sha256,
            expected_store_kind=expected_store_kind,
        )
        try:
            yield held.snapshot
        finally:
            with self._lock:
                entries = self._held.get(str(result_ref), [])
                if held in entries:
                    entries.remove(held)
                if not entries:
                    self._held.pop(str(result_ref), None)
            os.close(held.data_descriptor)
            os.close(held.manifest_descriptor)

    def _open_and_register(self, **kwargs) -> _HeldDurableResult:
        result_ref = str(kwargs["result_ref"])
        with self._lock:
            stored = self._store._results.get(result_ref)
            held = _open_durable_result_snapshot(stored, **kwargs)
            self._held.setdefault(result_ref, []).append(held)
            return held

    async def delete_lifecycle_files(
        self,
        lifecycle: MCPDurableResultLifecycle,
        *,
        fault_hook: Callable[[str, MCPDurableResultLifecycle], None] | None = None,
    ) -> bool:
        return await asyncio.to_thread(
            self._delete_lifecycle_files_sync,
            lifecycle,
            fault_hook,
        )

    def _delete_lifecycle_files_sync(
        self,
        lifecycle: MCPDurableResultLifecycle,
        fault_hook: Callable[[str, MCPDurableResultLifecycle], None] | None,
    ) -> bool:
        with self._lock:
            if self._held.get(lifecycle.result_ref):
                return False
            data_path, manifest_path = _resolve_durable_lifecycle_paths(
                self._store.root,
                lifecycle,
            )
            for path, expected_sha, point in (
                (
                    manifest_path,
                    lifecycle.manifest_file_sha256,
                    "manifest_unlink",
                ),
                (data_path, lifecycle.data_file_sha256, "data_unlink"),
            ):
                if path is None:
                    continue
                if _sha256_result_lifecycle_file(path) != expected_sha:
                    raise MCPTemporaryResultError(
                        "Durable MCP result lifecycle digest changed."
                    )
                path.unlink()
                if fault_hook is not None:
                    fault_hook(point, lifecycle)
            parent = (
                data_path.parent
                if data_path is not None
                else manifest_path.parent
                if manifest_path is not None
                else None
            )
            if parent is not None:
                _fsync_directory(parent)
            self._store._results.pop(lifecycle.result_ref, None)
            return True

    def revalidate(
        self, snapshot: MCPDurableResultSnapshot
    ) -> MCPDurableResultSnapshot:
        with self._lock:
            entries = tuple(self._held.get(snapshot.result_ref, ()))
        for held in entries:
            if held.snapshot != snapshot:
                continue
            try:
                data = os.fstat(held.data_descriptor)
                manifest = os.fstat(held.manifest_descriptor)
                data_path = os.stat(held.data_path, follow_symlinks=False)
                manifest_path = os.stat(held.manifest_path, follow_symlinks=False)
            except OSError as exc:
                raise MCPTemporaryResultError(
                    "Durable MCP result descriptor identity changed."
                ) from exc
            if (
                _result_file_identity(data) != held.data_identity
                or _result_file_identity(data_path) != held.data_identity
                or _result_file_identity(manifest) != held.manifest_identity
                or _result_file_identity(manifest_path) != held.manifest_identity
            ):
                raise MCPTemporaryResultError(
                    "Durable MCP result descriptor identity changed."
                )
            return snapshot
        raise MCPTemporaryResultError(
            "Durable MCP result descriptor is not held."
        )

    def list_result_identities(
        self,
        *,
        after_result_ref: str | None = None,
        limit: int = 1000,
    ) -> tuple[MCPDurableResultIdentity, ...]:
        return self._store.list_durable_result_identities(
            after_result_ref=after_result_ref,
            limit=limit,
        )

    async def verify_completed_result(
        self,
        *,
        call: MCPCallRecord,
        receipt: MCPTerminalResultReceipt,
    ) -> str:
        if (
            receipt.safe_result_ref is None
            or receipt.safe_result_size_bytes is None
            or receipt.safe_result_content_sha256 is None
            or receipt.safe_result_store_kind is None
        ):
            raise MCPTemporaryResultError(
                "Durable MCP result receipt is incomplete."
            )
        async with self.open_snapshot(
            result_ref=receipt.safe_result_ref,
            owner_user_id=call.owner_user_id,
            task_id=call.task_id,
            node_id=call.node_id,
            call_id=call.call_ref,
            expected_size_bytes=receipt.safe_result_size_bytes,
            expected_content_sha256=receipt.safe_result_content_sha256,
            expected_store_kind=receipt.safe_result_store_kind,
        ):
            return receipt.safe_result_ref

    async def copy_to_artifact_file(
        self,
        *,
        result_ref: str,
        owner_user_id: str,
        task_id: str,
        node_id: str,
        call_id: str,
        expected_size_bytes: int,
        expected_content_sha256: str,
        expected_store_kind: str,
        file_store,
        artifact_id: str,
        filename: str,
    ):
        async with self.open_snapshot(
            result_ref=result_ref,
            owner_user_id=owner_user_id,
            task_id=task_id,
            node_id=node_id,
            call_id=call_id,
            expected_size_bytes=expected_size_bytes,
            expected_content_sha256=expected_content_sha256,
            expected_store_kind=expected_store_kind,
        ) as snapshot:
            with self._lock:
                held = next(
                    (
                        item
                        for item in self._held.get(result_ref, ())
                        if item.snapshot == snapshot
                    ),
                    None,
                )
            if held is None:
                raise MCPTemporaryResultError(
                    "Durable MCP result snapshot is not held."
                )
            stored = await asyncio.to_thread(
                file_store.save_file,
                artifact_id=artifact_id,
                filename=filename,
                source_path=held.data_path,
            )
            self.revalidate(snapshot)
            if (
                stored.size_bytes != snapshot.size_bytes
                or "sha256:" + stored.sha256 != snapshot.content_sha256
            ):
                raise MCPTemporaryResultError(
                    "Durable MCP result artifact copy verification failed."
                )
            return snapshot, stored


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


@dataclass(slots=True)
class _AdmissionRequest:
    owner_user_id: str
    request_ref: str
    granted: asyncio.Future["MCPAdmissionLease"]


class MCPAdmissionLease:
    """One active per-instance MCP network slot."""

    def __init__(self, queue: "MCPFairAdmissionQueue", request_ref: str) -> None:
        self._queue = queue
        self.request_ref = request_ref
        self._released = False

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        await self._queue.release(self.request_ref)

    async def __aenter__(self) -> "MCPAdmissionLease":
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback
        await self.release()


class MCPFairAdmissionQueue:
    """In-memory round-robin admission across users and FIFO within each user."""

    def __init__(
        self,
        *,
        max_active: int,
        disk_available: Callable[[], bool],
    ) -> None:
        if (
            isinstance(max_active, bool)
            or not isinstance(max_active, int)
            or max_active <= 0
        ):
            raise ValueError("max_active must be a positive integer.")
        self._max_active = max_active
        self._disk_available = disk_available
        self._active_refs: set[str] = set()
        self._pending_by_owner: dict[str, deque[_AdmissionRequest]] = {}
        self._owner_cycle: deque[str] = deque()
        self._pending_by_ref: dict[str, _AdmissionRequest] = {}
        self._lock = asyncio.Lock()

    @property
    def active_count(self) -> int:
        return len(self._active_refs)

    @property
    def queued_count(self) -> int:
        return len(self._pending_by_ref)

    async def acquire(
        self,
        owner_user_id: str,
        request_ref: str,
        *,
        on_queued: Callable[[int], Awaitable[None]] | None = None,
        on_admitted: Callable[[], Awaitable[None]] | None = None,
    ) -> MCPAdmissionLease:
        owner = str(owner_user_id).strip()
        ref = str(request_ref).strip()
        if not owner or not ref:
            raise ValueError("owner_user_id and request_ref are required.")
        loop = asyncio.get_running_loop()
        request = _AdmissionRequest(owner, ref, loop.create_future())
        async with self._lock:
            if ref in self._active_refs or ref in self._pending_by_ref:
                raise ValueError(
                    "request_ref must be unique while admission is active or queued."
                )
            if not self._has_disk_capacity():
                raise MCPCapacityUnavailableError()
            owner_queue = self._pending_by_owner.get(owner)
            if owner_queue is None:
                owner_queue = deque()
                self._pending_by_owner[owner] = owner_queue
                self._owner_cycle.append(owner)
            owner_queue.append(request)
            self._pending_by_ref[ref] = request
            self._drain_locked()
            queue_position = (
                len(self._pending_by_ref) if not request.granted.done() else None
            )
        try:
            if queue_position is not None and on_queued is not None:
                await on_queued(queue_position)
            lease = await request.granted
            if queue_position is not None and on_admitted is not None:
                await on_admitted()
            return lease
        except BaseException:
            if request.granted.done() and not request.granted.cancelled():
                lease = request.granted.result()
                await lease.release()
            else:
                await self.cancel(ref)
            raise

    async def try_acquire(
        self, owner_user_id: str, request_ref: str
    ) -> MCPAdmissionLease:
        owner = str(owner_user_id).strip()
        ref = str(request_ref).strip()
        if not owner or not ref:
            raise ValueError("owner_user_id and request_ref are required.")
        async with self._lock:
            if ref in self._active_refs or ref in self._pending_by_ref:
                raise ValueError(
                    "request_ref must be unique while admission is active or queued."
                )
            if (
                self._pending_by_ref
                or len(self._active_refs) >= self._max_active
                or not self._has_disk_capacity()
            ):
                raise MCPCapacityUnavailableError()
            self._active_refs.add(ref)
            return MCPAdmissionLease(self, ref)

    async def cancel(self, request_ref: str) -> bool:
        ref = str(request_ref)
        async with self._lock:
            request = self._pending_by_ref.pop(ref, None)
            if request is None:
                return False
            owner_queue = self._pending_by_owner[request.owner_user_id]
            owner_queue.remove(request)
            if not owner_queue:
                self._pending_by_owner.pop(request.owner_user_id, None)
                try:
                    self._owner_cycle.remove(request.owner_user_id)
                except ValueError:
                    pass
            if not request.granted.done():
                request.granted.set_exception(MCPAdmissionCancelledError())
            return True

    async def release(self, request_ref: str) -> None:
        ref = str(request_ref)
        async with self._lock:
            if ref not in self._active_refs:
                return
            self._active_refs.remove(ref)
            self._drain_locked()

    def _drain_locked(self) -> None:
        while self._owner_cycle and len(self._active_refs) < self._max_active:
            if not self._has_disk_capacity():
                self._fail_pending_locked()
                return
            owner = self._owner_cycle.popleft()
            owner_queue = self._pending_by_owner[owner]
            request = owner_queue.popleft()
            self._pending_by_ref.pop(request.request_ref, None)
            if owner_queue:
                self._owner_cycle.append(owner)
            else:
                self._pending_by_owner.pop(owner, None)
            if request.granted.cancelled():
                continue
            self._active_refs.add(request.request_ref)
            request.granted.set_result(MCPAdmissionLease(self, request.request_ref))

    def _fail_pending_locked(self) -> None:
        for request in self._pending_by_ref.values():
            if not request.granted.done():
                request.granted.set_exception(MCPCapacityUnavailableError())
        self._pending_by_ref.clear()
        self._pending_by_owner.clear()
        self._owner_cycle.clear()

    def _has_disk_capacity(self) -> bool:
        try:
            return bool(self._disk_available())
        except Exception:
            return False


class MCPTemporaryResultCapacity:
    """Disk-aware admission capacity with a keyed fair-queue API.

    ``admit()`` remains the legacy fail-fast context manager. New user-scoped
    execution should use ``acquire()`` so callers queue before constructing a
    client or decrypting credentials.
    """

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
        self._fair_queue = MCPFairAdmissionQueue(
            max_active=config.max_active_user_mcp_calls_per_instance,
            disk_available=self._has_disk_capacity,
        )

    @property
    def active_calls(self) -> int:
        return self._fair_queue.active_count

    @property
    def queued_calls(self) -> int:
        return self._fair_queue.queued_count

    async def acquire(
        self,
        owner_user_id: str,
        request_ref: str,
        *,
        on_queued: Callable[[int], Awaitable[None]] | None = None,
        on_admitted: Callable[[], Awaitable[None]] | None = None,
    ) -> MCPAdmissionLease:
        return await self._fair_queue.acquire(
            owner_user_id,
            request_ref,
            on_queued=on_queued,
            on_admitted=on_admitted,
        )

    async def cancel(self, request_ref: str) -> bool:
        return await self._fair_queue.cancel(request_ref)

    @asynccontextmanager
    async def admit(self) -> AsyncIterator[None]:
        lease = await self._fair_queue.try_acquire(
            "__legacy__", f"mcp-legacy-admission-{secrets.token_urlsafe(18)}"
        )
        try:
            yield
        finally:
            await lease.release()

    def _has_disk_capacity(self) -> bool:
        return (
            self._free_bytes(self._storage_root)
            >= self._config.temporary_disk_low_watermark_bytes
        )


@dataclass(slots=True)
class _StoredResult:
    task_key: str
    task_id: str
    scope_id: str | None
    owner_user_id: str | None
    node_id: str | None
    call_ref: str | None
    size_bytes: int
    sha256: str
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
        self._spill_observer: Callable[[str | None, int], Awaitable[None]] | None = None
        self._cleanup_failure_observer: Callable[[str | None], Awaitable[None]] | None = None
        self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
        _secure_private_root(self._root)
        self._load_durable_results()

    @property
    def root(self) -> Path:
        return self._root

    def configure_metric_observers(
        self,
        *,
        spill_observer: Callable[[str | None, int], Awaitable[None]] | None,
        cleanup_failure_observer: Callable[[str | None], Awaitable[None]] | None,
    ) -> None:
        self._spill_observer = spill_observer
        self._cleanup_failure_observer = cleanup_failure_observer

    async def _observe_spill_bytes(
        self,
        scope_id: str | None,
    ) -> None:
        if self._spill_observer is None:
            return
        try:
            await self._spill_observer(
                scope_id,
                sum(
                    stored.size_bytes
                    for stored in self._results.values()
                    if stored.scope_id == scope_id and stored.path is not None
                ),
            )
        except Exception:
            return

    async def _observe_cleanup_failure(self, scope_id: str | None) -> None:
        if self._cleanup_failure_observer is None:
            return
        try:
            await self._cleanup_failure_observer(scope_id)
        except Exception:
            return

    def create_sink(
        self,
        task_id: str,
        *,
        scope_id: str | None = None,
        durable: bool = False,
        owner_user_id: str | None = None,
        node_id: str | None = None,
        call_ref: str | None = None,
    ) -> MCPResultSink:
        if durable:
            durable_identity = {
                "task_id": task_id,
                "owner_user_id": owner_user_id,
                "node_id": node_id,
                "call_ref": call_ref,
            }
            for field_name, value in durable_identity.items():
                if value is None or not str(value).strip():
                    raise ValueError(
                        f"{field_name} is required for a durable MCP result."
                    )
        task_key = self._task_key(task_id)
        return _MemoryToFileSink(
            store=self,
            task_id=str(task_id),
            task_key=task_key,
            scope_id=str(scope_id) if scope_id is not None else None,
            owner_user_id=(
                str(owner_user_id) if owner_user_id is not None else None
            ),
            node_id=str(node_id) if node_id is not None else None,
            call_ref=str(call_ref) if call_ref is not None else None,
            threshold=self._memory_threshold_bytes,
            durable=durable,
            maximum_bytes=(MAX_DURABLE_MCP_RESULT_BYTES if durable else None),
        )

    def resolve_ref(self, ref: str) -> MCPTemporaryResultRef:
        stored = self._results.get(str(ref))
        if stored is None:
            raise KeyError("Unknown or expired MCP temporary result reference.")
        return MCPTemporaryResultRef(
            ref=str(ref),
            size_bytes=stored.size_bytes,
            sha256=stored.sha256,
            storage="memory" if stored.memory is not None else "file",
        )

    async def verify_durable_ref(
        self,
        ref: str,
        *,
        owner_user_id: str,
        task_id: str,
        node_id: str,
        call_ref: str,
        scope_id: str | None,
        expected_size_bytes: int,
        expected_sha256: str,
        expected_store_kind: str,
    ) -> MCPTemporaryResultRef:
        stored = self._results.get(str(ref))
        if (
            stored is None
            or not stored.promoted
            or stored.path is None
            or stored.memory is not None
            or stored.owner_user_id != str(owner_user_id)
            or stored.task_id != str(task_id)
            or stored.node_id != str(node_id)
            or stored.call_ref != str(call_ref)
            or stored.scope_id != (str(scope_id) if scope_id is not None else None)
            or stored.size_bytes != expected_size_bytes
            or stored.sha256 != expected_sha256
            or expected_store_kind != "durable_content_addressed"
        ):
            raise MCPTemporaryResultError(
                "Durable MCP result authority identity verification failed."
            )
        manifest_path = stored.path.with_suffix(".manifest.json")
        manifest = await asyncio.to_thread(_read_private_json, manifest_path)
        parsed = _parse_durable_result_manifest(manifest)
        expected = (
            str(ref),
            expected_size_bytes,
            expected_sha256,
            str(task_id),
            str(scope_id) if scope_id is not None else None,
            str(owner_user_id),
            str(node_id),
            str(call_ref),
        )
        if parsed != expected or manifest_path.name != f"{ref}.manifest.json":
            raise MCPTemporaryResultError(
                "Durable MCP result manifest authority identity drifted."
            )
        if not await asyncio.to_thread(
            _file_matches_result,
            stored.path,
            expected_size_bytes,
            expected_sha256,
        ):
            raise MCPTemporaryResultError(
                "Durable MCP result integrity verification failed."
            )
        return MCPTemporaryResultRef(
            ref=str(ref),
            size_bytes=expected_size_bytes,
            sha256=expected_sha256,
            storage="file",
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
        if not await asyncio.to_thread(
            _file_matches_result, stored.path, stored.size_bytes, stored.sha256
        ):
            raise MCPTemporaryResultError(
                "Durable MCP result integrity verification failed."
            )
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
            try:
                await _unlink(stored.path)
            except BaseException:
                await self._observe_cleanup_failure(stored.scope_id)
                raise
            await self._observe_spill_bytes(stored.scope_id)

    async def cleanup_scope(self, scope_id: str) -> None:
        normalized = str(scope_id)
        removed_file = False
        for ref, stored in list(self._results.items()):
            if stored.scope_id == normalized and not stored.promoted:
                self._results.pop(ref, None)
                if stored.path is not None:
                    try:
                        await _unlink(stored.path)
                    except BaseException:
                        await self._observe_cleanup_failure(stored.scope_id)
                        raise
                    removed_file = True
        if removed_file:
            await self._observe_spill_bytes(normalized)

    async def cleanup_task(self, task_id: str) -> None:
        normalized_task_id = str(task_id)
        task_key = self._tasks.get(normalized_task_id)
        if task_key is None:
            return
        removed_scope_ids: set[str | None] = set()
        for ref, stored in list(self._results.items()):
            if stored.task_key == task_key and not stored.promoted:
                self._results.pop(ref, None)
                if stored.path is not None:
                    try:
                        await _unlink(stored.path)
                    except BaseException:
                        await self._observe_cleanup_failure(stored.scope_id)
                        raise
                    removed_scope_ids.add(stored.scope_id)
        for scope_id in removed_scope_ids:
            await self._observe_spill_bytes(scope_id)
        if any(stored.task_key == task_key for stored in self._results.values()):
            return
        self._tasks.pop(normalized_task_id, None)
        task_dir = self._root / task_key
        if task_dir.exists() and not any(stored.task_key == task_key for stored in self._results.values()):
            await asyncio.to_thread(shutil.rmtree, task_dir, True)

    def active_task_keys(self) -> frozenset[str]:
        return frozenset(self._tasks.values())

    def list_durable_result_identities(
        self,
        *,
        after_result_ref: str | None = None,
        limit: int = 1000,
    ) -> tuple[MCPDurableResultIdentity, ...]:
        if isinstance(limit, bool) or limit < 1 or limit > 1000:
            raise ValueError("mcp_durable_result_inventory_limit_invalid")
        values: list[MCPDurableResultIdentity] = []
        for result_ref, stored in sorted(self._results.items()):
            if after_result_ref is not None and result_ref <= after_result_ref:
                continue
            if not stored.promoted or stored.path is None:
                continue
            values.append(
                MCPDurableResultIdentity(
                    result_ref=result_ref,
                    owner_user_id=stored.owner_user_id,
                    task_id=stored.task_id,
                    node_id=stored.node_id,
                    call_id=stored.call_ref,
                    size_bytes=stored.size_bytes,
                    content_sha256="sha256:" + stored.sha256,
                )
            )
            if len(values) == limit:
                break
        return tuple(values)

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

    def _load_durable_results(self) -> None:
        for manifest_path in sorted(self._root.glob("task-*/*.manifest.json")):
            try:
                task_key = manifest_path.parent.name
                _validate_private_directory(manifest_path.parent)
                manifest = _read_private_json(manifest_path)
                (
                    ref,
                    size_bytes,
                    sha256,
                    task_id,
                    scope_id,
                    owner_user_id,
                    node_id,
                    call_ref,
                ) = _parse_durable_result_manifest(manifest)
                if manifest_path.name != f"{ref}.manifest.json":
                    raise MCPTemporaryResultError(
                        "Durable MCP result manifest filename does not match its reference."
                    )
                data_path = manifest_path.parent / f"{ref}.json"
                if not _file_matches_result(data_path, size_bytes, sha256):
                    raise MCPTemporaryResultError(
                        "Durable MCP result integrity verification failed."
                    )
                task_key = manifest_path.parent.name
                existing = self._results.get(ref)
                if existing is not None:
                    raise MCPTemporaryResultError(
                        "Durable MCP result reference is duplicated."
                    )
                existing_task_key = self._tasks.get(task_id)
                if existing_task_key is not None and existing_task_key != task_key:
                    raise MCPTemporaryResultError(
                        "Durable MCP result task identity is duplicated."
                    )
            except MCPTemporaryResultError:
                raise
            except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise MCPTemporaryResultError(
                    "Durable MCP result manifest validation failed."
                ) from exc
            self._results[ref] = _StoredResult(
                task_key=task_key,
                task_id=task_id,
                scope_id=str(scope_id) if scope_id is not None else None,
                owner_user_id=owner_user_id,
                node_id=node_id,
                call_ref=call_ref,
                size_bytes=size_bytes,
                sha256=sha256,
                memory=None,
                path=data_path,
                promoted=True,
            )
            self._tasks[task_id] = task_key

    async def _remove_empty_task_dir(self, task_key: str) -> None:
        task_dir = self._root / task_key
        if task_dir.exists() and not any(stored.task_key == task_key for stored in self._results.values()):
            await asyncio.to_thread(shutil.rmtree, task_dir, True)


class _MemoryToFileSink:
    def __init__(
        self,
        *,
        store: MCPTemporaryResultStore,
        task_id: str,
        task_key: str,
        scope_id: str | None,
        owner_user_id: str | None,
        node_id: str | None,
        call_ref: str | None,
        threshold: int,
        durable: bool,
        maximum_bytes: int | None,
    ) -> None:
        self._store = store
        self._task_id = task_id
        self._task_key = task_key
        self._scope_id = scope_id
        self._owner_user_id = owner_user_id
        self._node_id = node_id
        self._call_ref = call_ref
        self._threshold = threshold
        self._durable = durable
        self._maximum_bytes = maximum_bytes
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
        if (
            self._maximum_bytes is not None
            and self._size + len(data) > self._maximum_bytes
        ):
            await self.abort()
            raise MCPResultTooLargeError()
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
            if self._durable and self._handle is None:
                await self._spill()
            if self._handle is not None:
                await self._flush_file_buffer()
                await asyncio.to_thread(self._handle.flush)
                await asyncio.to_thread(os.fsync, self._handle.fileno())
                await asyncio.to_thread(self._handle.close)
                self._handle = None
        except OSError as exc:
            await self.abort()
            if exc.errno in {errno.ENOSPC, errno.EDQUOT}:
                raise MCPTemporaryStorageExhaustedError() from exc
            raise
        self._finished = True
        result = MCPTemporaryResultRef(
            ref=(
                "mcp-result-"
                f"{self._task_key.removeprefix('task-')}-"
                f"{hashlib.sha256(str(self._call_ref or self._scope_id or '').encode()).hexdigest()}-"
                f"{self._digest.hexdigest()}"
                if self._durable
                else f"mcp-result-{secrets.token_urlsafe(24)}"
            ),
            size_bytes=self._size,
            sha256=self._digest.hexdigest(),
            storage="file" if self._path is not None else "memory",
        )
        if self._durable:
            assert self._path is not None
            final_path = self._path.with_name(f"{result.ref}.json")
            temporary_path = self._path
            try:
                await asyncio.to_thread(
                    _publish_durable_data,
                    temporary_path,
                    final_path,
                    result.size_bytes,
                    result.sha256,
                )
            except BaseException:
                await _unlink(temporary_path)
                raise
            self._path = final_path
            manifest_path = final_path.with_suffix(".manifest.json")
            manifest_payload = json.dumps(
                {
                    "schema": "maf.user_mcp.durable_result_manifest.v2",
                    "ref": result.ref,
                    "size_bytes": result.size_bytes,
                    "sha256": result.sha256,
                    "store_kind": "durable_content_addressed",
                    "owner_user_id": self._owner_user_id,
                    "task_id": self._task_id,
                    "node_id": self._node_id,
                    "call_ref": self._call_ref,
                    "scope_id": self._scope_id,
                },
                separators=(",", ":"),
                sort_keys=True,
            )
            await asyncio.to_thread(
                _publish_or_compare_text,
                manifest_path,
                manifest_payload,
            )
        self._store._register(
            result,
            _StoredResult(
                task_key=self._task_key,
                task_id=self._task_id,
                scope_id=self._scope_id,
                owner_user_id=self._owner_user_id,
                node_id=self._node_id,
                call_ref=self._call_ref,
                size_bytes=result.size_bytes,
                sha256=result.sha256,
                memory=None if self._path is not None else bytes(self._memory),
                path=self._path,
                promoted=self._durable,
            ),
        )
        self._memory.clear()
        self._file_buffer.clear()
        if result.storage == "file":
            await self._store._observe_spill_bytes(self._scope_id)
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
            try:
                await _unlink(self._path)
            except BaseException:
                await self._store._observe_cleanup_failure(self._scope_id)
                raise

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


def _resolve_durable_lifecycle_paths(
    root: Path,
    lifecycle: MCPDurableResultLifecycle,
) -> tuple[Path | None, Path | None]:
    expected_names = {
        lifecycle.data_filename: f"{lifecycle.result_ref}.json",
        lifecycle.manifest_filename: f"{lifecycle.result_ref}.manifest.json",
    }
    for actual, expected in expected_names.items():
        if (
            actual != expected
            or Path(actual).name != actual
            or "/" in actual
            or "\\" in actual
        ):
            raise MCPTemporaryResultError(
                "Durable MCP result lifecycle filename is invalid."
            )

    def find(name: str) -> Path | None:
        matches = []
        for task_dir in root.iterdir():
            if not task_dir.name.startswith("task-"):
                continue
            try:
                _validate_private_directory(task_dir)
            except MCPTemporaryResultError:
                raise
            candidate = task_dir / name
            if candidate.exists() or candidate.is_symlink():
                matches.append(candidate)
        if len(matches) > 1:
            raise MCPTemporaryResultError(
                "Durable MCP result lifecycle file is duplicated."
            )
        return None if not matches else matches[0]

    data_path = find(lifecycle.data_filename)
    manifest_path = find(lifecycle.manifest_filename)
    if (
        data_path is not None
        and manifest_path is not None
        and data_path.parent != manifest_path.parent
    ):
        raise MCPTemporaryResultError(
            "Durable MCP result lifecycle files are forked."
        )
    return data_path, manifest_path


def _sha256_result_lifecycle_file(path: Path) -> str:
    metadata = path.stat(follow_symlinks=False)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
    ):
        raise MCPTemporaryResultError(
            "Durable MCP result lifecycle file identity is invalid."
        )
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
    )
    digest = hashlib.sha256()
    with os.fdopen(descriptor, "rb") as handle:
        opened = os.fstat(handle.fileno())
        if _result_file_identity(opened) != _result_file_identity(metadata):
            raise MCPTemporaryResultError(
                "Durable MCP result lifecycle file identity changed."
            )
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
        after = os.fstat(handle.fileno())
    after_path = path.stat(follow_symlinks=False)
    if (
        _result_file_identity(after) != _result_file_identity(metadata)
        or _result_file_identity(after_path) != _result_file_identity(metadata)
    ):
        raise MCPTemporaryResultError(
            "Durable MCP result lifecycle file identity changed."
        )
    return "sha256:" + digest.hexdigest()


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
            if any(candidate.glob("*.manifest.json")):
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


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _file_matches_result(
    path: Path, size_bytes: int, expected_sha256: str
) -> bool:
    try:
        metadata = path.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
            or metadata.st_size != size_bytes
        ):
            return False
        digest = hashlib.sha256()
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if (
                opened.st_dev != metadata.st_dev
                or opened.st_ino != metadata.st_ino
                or not stat.S_ISREG(opened.st_mode)
                or opened.st_uid != os.getuid()
                or stat.S_IMODE(opened.st_mode) != 0o600
                or opened.st_nlink != 1
                or opened.st_size != size_bytes
            ):
                return False
            while chunk := handle.read(64 * 1024):
                digest.update(chunk)
        return digest.hexdigest() == expected_sha256
    except OSError:
        return False


async def _unlink(path: Path) -> None:
    try:
        await asyncio.to_thread(path.unlink)
    except FileNotFoundError:
        pass


def _publish_durable_data(
    temporary_path: Path,
    final_path: Path,
    size_bytes: int,
    sha256: str,
) -> None:
    try:
        os.link(temporary_path, final_path, follow_symlinks=False)
    except FileExistsError:
        if not _file_matches_result(final_path, size_bytes, sha256):
            raise MCPTemporaryResultError(
                "Durable MCP result reference conflicts with existing content."
            )
    else:
        _fsync_directory(final_path.parent)
    temporary_path.unlink()
    _fsync_directory(final_path.parent)
    if not _file_matches_result(final_path, size_bytes, sha256):
        raise MCPTemporaryResultError(
            "Durable MCP result integrity verification failed."
        )


def _publish_or_compare_text(path: Path, value: str) -> None:
    encoded = value.encode("utf-8")
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
    except FileExistsError:
        try:
            existing = _read_private_file(path)
        except MCPTemporaryResultError:
            existing = None
        if existing != encoded:
            raise MCPTemporaryResultError(
                "Durable MCP result manifest conflicts with existing content."
            )
        return
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        raise
    _fsync_directory(path.parent)


def _validate_private_directory(path: Path) -> None:
    metadata = path.stat(follow_symlinks=False)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise MCPTemporaryResultError(
            "Durable MCP result directory identity verification failed."
        )


def _secure_private_root(path: Path) -> None:
    metadata = path.stat(follow_symlinks=False)
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
        raise MCPTemporaryResultError(
            "Durable MCP result root identity verification failed."
        )
    os.chmod(path, 0o700, follow_symlinks=False)
    secured = path.stat(follow_symlinks=False)
    if (
        secured.st_dev != metadata.st_dev
        or secured.st_ino != metadata.st_ino
        or not stat.S_ISDIR(secured.st_mode)
        or secured.st_uid != os.getuid()
        or stat.S_IMODE(secured.st_mode) != 0o700
    ):
        raise MCPTemporaryResultError(
            "Durable MCP result root changed during security setup."
        )


def _read_private_file(path: Path) -> bytes:
    metadata = path.stat(follow_symlinks=False)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
    ):
        raise MCPTemporaryResultError(
            "Durable MCP result file identity verification failed."
        )
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    with os.fdopen(descriptor, "rb") as handle:
        opened = os.fstat(handle.fileno())
        if (
            opened.st_dev != metadata.st_dev
            or opened.st_ino != metadata.st_ino
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.getuid()
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_nlink != 1
        ):
            raise MCPTemporaryResultError(
                "Durable MCP result file identity changed while opening."
            )
        return handle.read()


def _open_durable_result_snapshot(
    stored: _StoredResult | None,
    *,
    result_ref: str,
    owner_user_id: str,
    task_id: str,
    node_id: str,
    call_id: str,
    expected_size_bytes: int,
    expected_content_sha256: str,
    expected_store_kind: str,
) -> _HeldDurableResult:
    if (
        stored is None
        or not stored.promoted
        or stored.path is None
        or stored.memory is not None
        or stored.owner_user_id != owner_user_id
        or stored.task_id != task_id
        or stored.node_id != node_id
        or stored.call_ref != call_id
        or stored.size_bytes != expected_size_bytes
        or expected_content_sha256 != f"sha256:{stored.sha256}"
        or expected_store_kind != "durable_content_addressed"
    ):
        raise MCPTemporaryResultError(
            "Durable MCP result snapshot identity verification failed."
        )
    data_path = stored.path
    manifest_path = data_path.with_suffix(".manifest.json")
    _validate_private_directory(data_path.parent)
    data_descriptor = -1
    manifest_descriptor = -1
    try:
        data_before = os.stat(data_path, follow_symlinks=False)
        manifest_before = os.stat(manifest_path, follow_symlinks=False)
        data_descriptor = os.open(
            data_path,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        manifest_descriptor = os.open(
            manifest_path,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        data_opened = os.fstat(data_descriptor)
        manifest_opened = os.fstat(manifest_descriptor)
        _validate_result_snapshot_file(data_opened, maximum_size=MAX_DURABLE_MCP_RESULT_BYTES)
        _validate_result_snapshot_file(manifest_opened, maximum_size=64 * 1024)
        if (
            _result_file_identity(data_before)
            != _result_file_identity(data_opened)
            or _result_file_identity(manifest_before)
            != _result_file_identity(manifest_opened)
        ):
            raise MCPTemporaryResultError(
                "Durable MCP result snapshot path identity changed."
            )
        digest = hashlib.sha256()
        remaining = data_opened.st_size
        while remaining:
            chunk = os.read(data_descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                raise MCPTemporaryResultError(
                    "Durable MCP result snapshot data was truncated."
                )
            digest.update(chunk)
            remaining -= len(chunk)
        if os.read(data_descriptor, 1):
            raise MCPTemporaryResultError(
                "Durable MCP result snapshot data grew during read."
            )
        manifest_bytes = _read_descriptor_exact(
            manifest_descriptor, manifest_opened.st_size
        )
        try:
            manifest_value = json.loads(manifest_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MCPTemporaryResultError(
                "Durable MCP result snapshot manifest is invalid."
            ) from exc
        if not isinstance(manifest_value, dict):
            raise MCPTemporaryResultError(
                "Durable MCP result snapshot manifest is invalid."
            )
        parsed = _parse_durable_result_manifest(manifest_value)
        expected_manifest = (
            result_ref,
            expected_size_bytes,
            stored.sha256,
            task_id,
            stored.scope_id,
            owner_user_id,
            node_id,
            call_id,
        )
        if (
            parsed != expected_manifest
            or data_path.name != f"{result_ref}.json"
            or manifest_path.name != f"{result_ref}.manifest.json"
            or digest.hexdigest() != stored.sha256
        ):
            raise MCPTemporaryResultError(
                "Durable MCP result snapshot authority drifted."
            )
        data_after = os.fstat(data_descriptor)
        manifest_after = os.fstat(manifest_descriptor)
        data_after_path = os.stat(data_path, follow_symlinks=False)
        manifest_after_path = os.stat(manifest_path, follow_symlinks=False)
        if (
            _result_file_identity(data_opened) != _result_file_identity(data_after)
            or _result_file_identity(data_after)
            != _result_file_identity(data_after_path)
            or _result_file_identity(manifest_opened)
            != _result_file_identity(manifest_after)
            or _result_file_identity(manifest_after)
            != _result_file_identity(manifest_after_path)
        ):
            raise MCPTemporaryResultError(
                "Durable MCP result snapshot identity changed during read."
            )
        snapshot = MCPDurableResultSnapshot(
            result_ref=result_ref,
            owner_user_id=owner_user_id,
            task_id=task_id,
            node_id=node_id,
            call_id=call_id,
            content_sha256=expected_content_sha256,
            size_bytes=expected_size_bytes,
            store_kind=expected_store_kind,
            data_filename=data_path.name,
            manifest_filename=manifest_path.name,
            data_file_sha256="sha256:" + stored.sha256,
            manifest_file_sha256=(
                "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()
            ),
            data_file_device=data_after.st_dev,
            data_file_inode=data_after.st_ino,
            data_file_mode=stat.S_IMODE(data_after.st_mode),
            data_file_owner_uid=data_after.st_uid,
            manifest_file_device=manifest_after.st_dev,
            manifest_file_inode=manifest_after.st_ino,
            manifest_file_mode=stat.S_IMODE(manifest_after.st_mode),
            manifest_file_owner_uid=manifest_after.st_uid,
        )
        held = _HeldDurableResult(
            snapshot=snapshot,
            data_descriptor=data_descriptor,
            manifest_descriptor=manifest_descriptor,
            data_path=data_path,
            manifest_path=manifest_path,
            data_identity=_result_file_identity(data_after),
            manifest_identity=_result_file_identity(manifest_after),
        )
        data_descriptor = -1
        manifest_descriptor = -1
        return held
    except OSError as exc:
        raise MCPTemporaryResultError(
            "Durable MCP result snapshot file is unsafe or missing."
        ) from exc
    finally:
        if data_descriptor >= 0:
            os.close(data_descriptor)
        if manifest_descriptor >= 0:
            os.close(manifest_descriptor)


def _validate_result_snapshot_file(
    value: os.stat_result, *, maximum_size: int
) -> None:
    if (
        not stat.S_ISREG(value.st_mode)
        or value.st_uid != os.getuid()
        or stat.S_IMODE(value.st_mode) != 0o600
        or value.st_nlink != 1
        or value.st_size < 0
        or value.st_size > maximum_size
    ):
        raise MCPTemporaryResultError(
            "Durable MCP result snapshot file identity is invalid."
        )


def _result_file_identity(value: os.stat_result) -> tuple[int, ...]:
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


def _read_descriptor_exact(descriptor: int, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = os.read(descriptor, min(remaining, 64 * 1024))
        if not chunk:
            raise MCPTemporaryResultError(
                "Durable MCP result snapshot manifest was truncated."
            )
        chunks.append(chunk)
        remaining -= len(chunk)
    if os.read(descriptor, 1):
        raise MCPTemporaryResultError(
            "Durable MCP result snapshot manifest grew during read."
        )
    return b"".join(chunks)


def _read_private_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(_read_private_file(path).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MCPTemporaryResultError(
            "Durable MCP result manifest is not valid JSON."
        ) from exc
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise MCPTemporaryResultError(
            "Durable MCP result manifest must be a JSON object."
        )
    return value


def _parse_durable_result_manifest(
    manifest: dict[str, object],
) -> tuple[str, int, str, str, str | None, str | None, str | None, str | None]:
    legacy_keys = {"ref", "scope_id", "sha256", "size_bytes", "task_id"}
    v2_keys = {
        "call_ref",
        "node_id",
        "owner_user_id",
        "ref",
        "schema",
        "scope_id",
        "sha256",
        "size_bytes",
        "store_kind",
        "task_id",
    }
    schema = manifest.get("schema")
    if schema is None:
        if set(manifest) != legacy_keys:
            raise MCPTemporaryResultError(
                "Legacy durable MCP result manifest has unknown fields."
            )
        owner_user_id = node_id = call_ref = None
    else:
        if schema != "maf.user_mcp.durable_result_manifest.v2":
            raise MCPTemporaryResultError(
                "Durable MCP result manifest schema is unsupported."
            )
        if set(manifest) != v2_keys:
            raise MCPTemporaryResultError(
                "Durable MCP result manifest has unknown or missing fields."
            )
        if manifest.get("store_kind") != "durable_content_addressed":
            raise MCPTemporaryResultError(
                "Durable MCP result store kind is invalid."
            )
        owner_user_id = _required_manifest_string(manifest, "owner_user_id")
        node_id = _required_manifest_string(manifest, "node_id")
        call_ref = _required_manifest_string(manifest, "call_ref")
    ref = _required_manifest_string(manifest, "ref")
    if not ref.startswith("mcp-result-") or any(
        character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
        for character in ref
    ):
        raise MCPTemporaryResultError(
            "Durable MCP result reference is invalid."
        )
    task_id = _required_manifest_string(manifest, "task_id")
    size_bytes = manifest.get("size_bytes")
    if (
        isinstance(size_bytes, bool)
        or not isinstance(size_bytes, int)
        or size_bytes < 0
        or size_bytes > MAX_DURABLE_MCP_RESULT_BYTES
    ):
        raise MCPTemporaryResultError(
            "Durable MCP result size is invalid."
        )
    sha256 = manifest.get("sha256")
    if (
        not isinstance(sha256, str)
        or len(sha256) != 64
        or any(character not in "0123456789abcdef" for character in sha256)
    ):
        raise MCPTemporaryResultError(
            "Durable MCP result digest is invalid."
        )
    scope_id = manifest.get("scope_id")
    if scope_id is not None and (
        not isinstance(scope_id, str) or not scope_id.strip()
    ):
        raise MCPTemporaryResultError(
            "Durable MCP result scope identity is invalid."
        )
    return (
        ref,
        size_bytes,
        sha256,
        task_id,
        scope_id,
        owner_user_id,
        node_id,
        call_ref,
    )


def _required_manifest_string(
    manifest: dict[str, object], field_name: str
) -> str:
    value = manifest.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise MCPTemporaryResultError(
            f"Durable MCP result {field_name} is invalid."
        )
    return value


__all__ = [
    "MCPAdmissionCancelledError",
    "MCPAdmissionLease",
    "MCPCapacityUnavailableError",
    "MCPFairAdmissionQueue",
    "MCPDurableResultIdentity",
    "MCPDurableResultSnapshotAuthority",
    "MCPResultTooLargeError",
    "MCPResultSink",
    "MCPTemporaryResultCapacity",
    "MCPTemporaryResultCapacityConfig",
    "MCPTemporaryResultError",
    "MCPTemporaryResultJanitor",
    "MCPTemporaryResultRef",
    "MCPTemporaryResultStore",
    "MCPTemporaryStorageExhaustedError",
    "MAX_DURABLE_MCP_RESULT_BYTES",
]
