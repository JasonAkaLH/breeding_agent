from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Generic, TypeVar

from .models import AgentLeaseLost, AgentRun, AgentTaskLease
from .repository import AgentTaskLeaseStore


T = TypeVar("T")
ACTIVE_LEASE_PHASES = frozenset({"model_sample", "compaction", "capability_wave", "final_publish"})


class AgentExecutionFailure(RuntimeError):
    """Carry this execution's live handle across the runtime error boundary."""

    def __init__(self, handle: AgentLeaseHandle, cause: Exception) -> None:
        super().__init__(str(cause))
        self.handle = handle
        self.cause = cause


@dataclass(slots=True)
class AgentLeaseHandle:
    current: AgentTaskLease
    _heartbeat_task: asyncio.Task[None] | None = field(
        default=None, init=False, repr=False
    )
    _ownership_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock,
        init=False,
        repr=False,
    )

    async def run_ownership_bound(
        self,
        operation: Callable[[AgentTaskLease], Awaitable[T]],
    ) -> T:
        async with self._ownership_lock:
            heartbeat = self._heartbeat_task
            if heartbeat is not None and heartbeat.done() and not heartbeat.cancelled():
                await heartbeat
            return await operation(self.current)

    def stop_heartbeat(self) -> None:
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()


class AgentLeaseController(Generic[T]):
    def __init__(
        self,
        store: AgentTaskLeaseStore,
        *,
        ttl_seconds: float,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if isinstance(ttl_seconds, bool) or ttl_seconds <= 0:
            raise ValueError("agent_task_lease_ttl_seconds must be positive")
        self._store = store
        self.ttl_seconds = float(ttl_seconds)
        self.heartbeat_interval_seconds = self.ttl_seconds / 3
        self._sleep = sleep

    async def acquire(self, run_id: str, *, owner_id: str) -> AgentLeaseHandle:
        lease = await self._store.acquire_task_lease(
            run_id,
            owner_id=owner_id,
            ttl_seconds=self.ttl_seconds,
        )
        return AgentLeaseHandle(lease)

    async def run_active_phase(
        self,
        phase: str,
        handle: AgentLeaseHandle,
        operation: Callable[[AgentLeaseHandle], Awaitable[T]],
    ) -> T:
        if phase not in ACTIVE_LEASE_PHASES:
            raise ValueError(f"unsupported Agent lease phase: {phase}")
        return await self.run_owned(handle, operation)

    async def run_owned(
        self,
        handle: AgentLeaseHandle,
        operation: Callable[[AgentLeaseHandle], Awaitable[T]],
    ) -> T:
        if handle._heartbeat_task is not None:
            if handle._heartbeat_task.done() and not handle._heartbeat_task.cancelled():
                await handle._heartbeat_task
            return await operation(handle)
        operation_task = asyncio.create_task(operation(handle))
        heartbeat_task = asyncio.create_task(self._heartbeat(handle))
        handle._heartbeat_task = heartbeat_task
        try:
            done, _ = await asyncio.wait(
                {operation_task, heartbeat_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if heartbeat_task in done and not heartbeat_task.cancelled():
                operation_task.cancel()
                await asyncio.gather(operation_task, return_exceptions=True)
                await heartbeat_task
                raise AgentLeaseLost("agent_task_lease_heartbeat_stopped")
            return await operation_task
        finally:
            operation_task.cancel()
            await asyncio.gather(operation_task, return_exceptions=True)
            # Finish a started renewal so its rotated token reaches the handle.
            async with handle._ownership_lock:
                heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
            handle._heartbeat_task = None

    async def release_waiting(self, run_id: str, *, handle: AgentLeaseHandle) -> AgentRun:
        async def release(lease: AgentTaskLease) -> AgentRun:
            run = await self._store.release_waiting_task_lease(
                run_id,
                owner_id=lease.owner_id,
                token=lease.token,
            )
            handle.stop_heartbeat()
            return run

        return await handle.run_ownership_bound(release)

    async def _heartbeat(self, handle: AgentLeaseHandle) -> None:
        while True:
            await self._sleep(self.heartbeat_interval_seconds)
            try:
                async def renew(lease: AgentTaskLease) -> None:
                    handle.current = await self._store.renew_task_lease(
                        lease.run_id,
                        owner_id=lease.owner_id,
                        token=lease.token,
                        ttl_seconds=self.ttl_seconds,
                    )

                await handle.run_ownership_bound(renew)
            except Exception as exc:
                raise AgentLeaseLost("agent_task_lease_lost") from exc
