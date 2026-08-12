from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from src.core.models import MCPConnectionLease


MCPPresenceCancellation = Callable[[str, str, str], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class MCPPresenceConnection:
    connection_id: str
    task_id: str
    owner_user_id: str
    auth_generation: int


class MCPTaskPresenceService:
    """Tracks authorized task SSE subscribers and enforces the offline grace period."""

    def __init__(
        self,
        *,
        cancel_mcp_task: MCPPresenceCancellation,
        grace_period_seconds: float = 300.0,
        storage: Any | None = None,
        instance_id: str = "local",
        lease_ttl_seconds: float = 45.0,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        if grace_period_seconds < 0:
            raise ValueError("grace_period_seconds must be non-negative")
        self._cancel_mcp_task = cancel_mcp_task
        self._grace_period_seconds = grace_period_seconds
        self._storage = storage
        self._instance_id = instance_id
        self._lease_ttl_seconds = lease_ttl_seconds
        self._now = now_fn or (lambda: datetime.now(timezone.utc).replace(tzinfo=None))
        self._connections: dict[str, MCPPresenceConnection] = {}
        self._task_connections: dict[tuple[str, str], set[str]] = defaultdict(set)
        self._grace_tasks: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    async def connect(self, connection: MCPPresenceConnection) -> None:
        key = (connection.owner_user_id, connection.task_id)
        async with self._lock:
            if self._closed:
                raise RuntimeError("MCP task presence service is closed")
            previous = self._connections.get(connection.connection_id)
            if previous is not None and previous != connection:
                raise ValueError("connection_id is already registered")
            self._connections[connection.connection_id] = connection
            self._task_connections[key].add(connection.connection_id)
            grace_task = self._grace_tasks.pop(key, None)
        if grace_task is not None:
            grace_task.cancel()
            await asyncio.gather(grace_task, return_exceptions=True)
        await self.heartbeat(connection.connection_id)

    async def heartbeat(self, connection_id: str) -> None:
        if self._storage is None:
            return
        async with self._lock:
            connection = self._connections.get(connection_id)
        if connection is None:
            return
        now = self._now()
        await self._storage.save_mcp_connection_lease(
            MCPConnectionLease(
                connection_id=connection.connection_id,
                owner_user_id=connection.owner_user_id,
                task_id=connection.task_id,
                instance_id=self._instance_id,
                lease_expires_at=now + timedelta(seconds=self._lease_ttl_seconds),
                auth_generation=connection.auth_generation,
                created_at=now,
                updated_at=now,
            )
        )

    async def disconnect(self, connection_id: str) -> None:
        local_connections_remain = False
        async with self._lock:
            connection = self._connections.pop(connection_id, None)
            if connection is None or self._closed:
                return
            key = (connection.owner_user_id, connection.task_id)
            task_connections = self._task_connections.get(key)
            if task_connections is None:
                return
            task_connections.discard(connection_id)
            if task_connections:
                local_connections_remain = True
            else:
                self._task_connections.pop(key, None)
        if self._storage is not None:
            await self._storage.delete_mcp_connection_lease(
                connection.owner_user_id,
                connection.task_id,
                connection.connection_id,
            )
        if local_connections_remain:
            return
        if self._storage is not None:
            live = await self._storage.list_live_mcp_connection_leases(
                connection.owner_user_id,
                connection.task_id,
                now=self._now(),
            )
            if live:
                return
        async with self._lock:
            if key in self._grace_tasks or self._task_connections.get(key):
                return
            self._grace_tasks[key] = asyncio.create_task(
                self._expire_after_grace(*key),
                name=f"mcp-presence-grace:{connection.task_id}",
            )

    async def invalidate_owner(
        self,
        owner_user_id: str,
        *,
        auth_generation: int | None = None,
        reason: str = "auth_invalidated",
    ) -> None:
        callbacks: list[tuple[str, str, str]] = []
        persistent_deletes: list[tuple[str, str, str]] = []
        grace_tasks: list[asyncio.Task[None]] = []
        async with self._lock:
            keys = {
                (connection.owner_user_id, connection.task_id)
                for connection in self._connections.values()
                if connection.owner_user_id == owner_user_id
                and (auth_generation is None or connection.auth_generation == auth_generation)
            }
            for key in keys:
                connection_ids = self._task_connections.pop(key, set())
                for connection_id in connection_ids:
                    self._connections.pop(connection_id, None)
                    persistent_deletes.append((key[0], key[1], connection_id))
                grace_task = self._grace_tasks.pop(key, None)
                if grace_task is not None:
                    grace_tasks.append(grace_task)
                callbacks.append((key[0], key[1], reason))
        for task in grace_tasks:
            task.cancel()
        if grace_tasks:
            await asyncio.gather(*grace_tasks, return_exceptions=True)
        if self._storage is not None:
            await asyncio.gather(
                *(
                    self._storage.delete_mcp_connection_lease(owner, task_id, connection_id)
                    for owner, task_id, connection_id in persistent_deletes
                ),
                return_exceptions=True,
            )
        await asyncio.gather(
            *(self._cancel_mcp_task(*callback) for callback in callbacks),
            return_exceptions=True,
        )

    async def aclose(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            tasks = list(self._grace_tasks.values())
            self._grace_tasks.clear()
            self._connections.clear()
            self._task_connections.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _expire_after_grace(self, owner_user_id: str, task_id: str) -> None:
        key = (owner_user_id, task_id)
        try:
            await asyncio.sleep(self._grace_period_seconds)
            if self._storage is not None:
                live = await self._storage.list_live_mcp_connection_leases(
                    owner_user_id,
                    task_id,
                    now=self._now(),
                )
                if live:
                    return
            async with self._lock:
                if self._closed or self._task_connections.get(key):
                    return
                current = self._grace_tasks.get(key)
                if current is not asyncio.current_task():
                    return
                self._grace_tasks.pop(key, None)
            await self._cancel_mcp_task(owner_user_id, task_id, "offline_grace_expired")
        except asyncio.CancelledError:
            raise

    async def active_connection_count(self, owner_user_id: str, task_id: str) -> int:
        async with self._lock:
            return len(self._task_connections.get((owner_user_id, task_id), ()))
