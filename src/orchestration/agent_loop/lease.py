from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

from .models import AgentLeaseLost, AgentRun, AgentTaskLease
from .repository import AgentTaskLeaseStore


T = TypeVar("T")
ACTIVE_LEASE_PHASES = frozenset({"model_sample", "compaction", "capability_wave", "final_publish"})


@dataclass(slots=True)
class AgentLeaseHandle:
    current: AgentTaskLease


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
        operation_task = asyncio.create_task(operation(handle))
        heartbeat_task = asyncio.create_task(self._heartbeat(handle))
        try:
            done, _ = await asyncio.wait(
                {operation_task, heartbeat_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        except asyncio.CancelledError:
            operation_task.cancel()
            heartbeat_task.cancel()
            await asyncio.gather(
                operation_task,
                heartbeat_task,
                return_exceptions=True,
            )
            raise
        if operation_task in done:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
            return await operation_task
        operation_task.cancel()
        await asyncio.gather(operation_task, return_exceptions=True)
        try:
            await heartbeat_task
        except AgentLeaseLost:
            raise
        except Exception as exc:
            raise AgentLeaseLost("agent_task_lease_heartbeat_failed") from exc
        raise AgentLeaseLost("agent_task_lease_heartbeat_stopped")

    async def release_waiting(self, run_id: str, *, handle: AgentLeaseHandle) -> AgentRun:
        return await self._store.release_waiting_task_lease(
            run_id,
            owner_id=handle.current.owner_id,
            token=handle.current.token,
        )

    async def _heartbeat(self, handle: AgentLeaseHandle) -> None:
        while True:
            await self._sleep(self.heartbeat_interval_seconds)
            try:
                handle.current = await self._store.renew_task_lease(
                    handle.current.run_id,
                    owner_id=handle.current.owner_id,
                    token=handle.current.token,
                    ttl_seconds=self.ttl_seconds,
                )
            except Exception as exc:
                raise AgentLeaseLost("agent_task_lease_lost") from exc
