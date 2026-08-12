from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Awaitable, Callable

import psycopg
from psycopg.conninfo import conninfo_to_dict
from sqlalchemy import Engine, text


MCP_INVALIDATION_CHANNEL = "maf_user_mcp_server_changed"


class MCPInvalidationAction(StrEnum):
    DISABLED = "disabled"
    SECURITY_UPDATED = "security_updated"
    DELETED = "deleted"


@dataclass(slots=True, frozen=True)
class MCPServerInvalidated:
    owner_user_id: str
    server_id: str
    security_version: int
    action: MCPInvalidationAction

    def public_payload(self) -> dict[str, object]:
        return {
            "owner_user_id": self.owner_user_id,
            "server_id": self.server_id,
            "security_version": self.security_version,
            "action": str(self.action),
        }


def validate_mcp_server_invalidation(payload: object) -> MCPServerInvalidated:
    if not isinstance(payload, dict):
        raise ValueError("MCP invalidation payload must be an object.")
    owner_user_id = str(payload.get("owner_user_id") or "").strip()
    server_id = str(payload.get("server_id") or "").strip()
    if not owner_user_id or not server_id:
        raise ValueError("MCP invalidation payload requires owner_user_id and server_id.")
    try:
        security_version = int(payload.get("security_version"))
    except (TypeError, ValueError) as exc:
        raise ValueError("MCP invalidation security_version must be an integer.") from exc
    if security_version < 0:
        raise ValueError("MCP invalidation security_version must be non-negative.")
    try:
        action = MCPInvalidationAction(str(payload.get("action") or ""))
    except ValueError as exc:
        raise ValueError("MCP invalidation action is unsupported.") from exc
    return MCPServerInvalidated(
        owner_user_id=owner_user_id,
        server_id=server_id,
        security_version=security_version,
        action=action,
    )


MCPInvalidationHandler = Callable[[MCPServerInvalidated], Awaitable[None] | None]


class InMemoryMCPInvalidationBus:
    def __init__(self) -> None:
        self._handlers: set[MCPInvalidationHandler] = set()

    def subscribe(self, handler: MCPInvalidationHandler) -> Callable[[], None]:
        self._handlers.add(handler)

        def unsubscribe() -> None:
            self._handlers.discard(handler)

        return unsubscribe

    async def publish(self, event: MCPServerInvalidated) -> None:
        validated = validate_mcp_server_invalidation(event.public_payload())
        for handler in tuple(self._handlers):
            result = handler(validated)
            if inspect.isawaitable(result):
                await result


class CompositeMCPInvalidationPublisher:
    def __init__(self, *publishers: Any) -> None:
        self._publishers = tuple(publisher for publisher in publishers if publisher is not None)

    async def publish(self, event: MCPServerInvalidated) -> None:
        for publisher in self._publishers:
            try:
                await publisher.publish(event)
            except Exception:
                # Durable version/lease fencing is authoritative; notification is acceleration only.
                continue


@dataclass(slots=True)
class MCPInvalidationListenerHealth:
    connected: bool = False
    reconnecting: bool = False
    last_notify_at: datetime | None = None
    last_error_code: str | None = None

    @property
    def ready(self) -> bool:
        return self.connected and not self.reconnecting and self.last_error_code is None


class PostgresMCPInvalidationBus:
    """Best-effort low-latency hint; database leases remain authoritative."""

    def __init__(self, engine: Engine, handler: MCPInvalidationHandler) -> None:
        self._engine = engine
        self._handler = handler
        self._dsn = _psycopg_conninfo_from_engine(engine)
        self.health = MCPInvalidationListenerHealth()
        self._closed = False
        self._listener_task: asyncio.Task[None] | None = None
        self._ready_event: asyncio.Event | None = None

    def check_permission(self) -> None:
        with self._engine.begin() as connection:
            connection.execute(text(f"LISTEN {MCP_INVALIDATION_CHANNEL}"))
            connection.execute(text(f"UNLISTEN {MCP_INVALIDATION_CHANNEL}"))

    async def publish(self, event: MCPServerInvalidated) -> None:
        sql, parameters = mcp_invalidation_notify_sql(event)
        await asyncio.to_thread(self._publish_sync, sql, parameters)

    def _publish_sync(self, sql: str, parameters: dict[str, Any]) -> None:
        with self._engine.begin() as connection:
            connection.execute(text(sql), parameters)

    async def start(self, *, ready_timeout_seconds: float = 5.0) -> None:
        if self._listener_task is not None and not self._listener_task.done():
            return
        self._closed = False
        self._ready_event = asyncio.Event()
        self._listener_task = asyncio.create_task(self._listen_loop(), name="user-mcp-invalidation-listener")
        try:
            await asyncio.wait_for(self._ready_event.wait(), timeout=ready_timeout_seconds)
        except Exception:
            await self.aclose()
            raise RuntimeError("PostgreSQL MCP invalidation listener did not become ready") from None

    async def _listen_loop(self) -> None:
        while not self._closed:
            try:
                if self._ready_event is not None:
                    self._ready_event.clear()
                self.health.connected = False
                self.health.reconnecting = True
                async with await psycopg.AsyncConnection.connect(self._dsn, autocommit=True) as connection:
                    await connection.execute(f"LISTEN {MCP_INVALIDATION_CHANNEL}")
                    self.health.connected = True
                    self.health.reconnecting = False
                    self.health.last_error_code = None
                    if self._ready_event is not None:
                        self._ready_event.set()
                    while not self._closed:
                        async for notify in connection.notifies(timeout=1.0, stop_after=1):
                            if self._closed:
                                break
                            try:
                                event = validate_mcp_server_invalidation(json.loads(notify.payload))
                                result = self._handler(event)
                                if inspect.isawaitable(result):
                                    await result
                            except (TypeError, ValueError):
                                self.health.last_error_code = "invalid_notify_payload"
                                continue
                            self.health.last_notify_at = datetime.now(timezone.utc)
                            self.health.last_error_code = None
            except asyncio.CancelledError:
                raise
            except Exception:
                self.health.connected = False
                self.health.reconnecting = True
                self.health.last_error_code = "listener_error"
                if self._ready_event is not None:
                    self._ready_event.clear()
                if not self._closed:
                    await asyncio.sleep(1.0)

    async def aclose(self) -> None:
        self._closed = True
        task = self._listener_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._listener_task = None
        if self._ready_event is not None:
            self._ready_event.clear()
        self.health.connected = False
        self.health.reconnecting = False


def mcp_invalidation_notify_sql(event: MCPServerInvalidated) -> tuple[str, dict[str, Any]]:
    validated = validate_mcp_server_invalidation(event.public_payload())
    payload = json.dumps(validated.public_payload(), ensure_ascii=False, sort_keys=True)
    return f"SELECT pg_notify('{MCP_INVALIDATION_CHANNEL}', :payload)", {"payload": payload}


def _psycopg_conninfo_from_engine(engine: Engine) -> str:
    url = engine.url
    if url.drivername.startswith("postgresql+"):
        url = url.set(drivername="postgresql")
    conninfo = url.render_as_string(hide_password=False)
    conninfo_to_dict(conninfo)
    return conninfo
