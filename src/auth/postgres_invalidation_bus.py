from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.conninfo import conninfo_to_dict
from sqlalchemy import Engine, text

from .generation_cache import AuthGenerationCache, AuthGenerationSnapshot
from .invalidation_bus import AuthGenerationChanged, validate_auth_generation_changed

CHANNEL = "maf_auth_generation_changed"


@dataclass(slots=True)
class AuthInvalidationListenerHealth:
    connected: bool = False
    reconnecting: bool = False
    last_reconcile_at: datetime | None = None
    last_notify_at: datetime | None = None
    last_error_code: str | None = None

    @property
    def ready(self) -> bool:
        return self.connected and not self.reconnecting and self.last_error_code is None

    def public_dict(self) -> dict[str, object]:
        return {
            "connected": self.connected,
            "reconnecting": self.reconnecting,
            "last_reconcile_at": self.last_reconcile_at.isoformat() if self.last_reconcile_at else None,
            "last_notify_at": self.last_notify_at.isoformat() if self.last_notify_at else None,
            "last_error_code": self.last_error_code,
            "ready": self.ready,
        }


class PostgresAuthInvalidationBus:
    """PostgreSQL LISTEN/NOTIFY auth generation invalidation helper."""

    def __init__(self, engine: Engine, cache: AuthGenerationCache) -> None:
        self._engine = engine
        self._cache = cache
        self._dsn = _psycopg_conninfo_from_engine(engine)
        self.health = AuthInvalidationListenerHealth()
        self._closed = False
        self._listener_task: asyncio.Task[None] | None = None
        self._ready_event: asyncio.Event | None = None

    def check_permission(self) -> None:
        with self._engine.begin() as connection:
            connection.execute(text(f"LISTEN {CHANNEL}"))
            connection.execute(text(f"UNLISTEN {CHANNEL}"))
        self.health.last_error_code = None

    def reconcile_once(self) -> None:
        snapshots: list[AuthGenerationSnapshot] = []
        with self._engine.begin() as connection:
            rows = connection.execute(
                text("SELECT username, auth_generation, auth_generation_updated_at FROM auth_user_token")
            ).mappings().all()
        for row in rows:
            snapshots.append(
                AuthGenerationSnapshot(
                    username=str(row["username"]),
                    auth_generation=int(row["auth_generation"] or 0),
                    updated_at=row.get("auth_generation_updated_at"),
                )
            )
        self._cache.reconcile(snapshots)
        self.health.last_reconcile_at = datetime.now(timezone.utc)
        self.health.last_error_code = None

    async def start(self, *, ready_timeout_seconds: float = 5.0) -> None:
        if self._listener_task is not None and not self._listener_task.done():
            return
        self._closed = False
        self._ready_event = asyncio.Event()
        self._listener_task = asyncio.create_task(self._listen_loop(), name="auth-generation-listener")
        try:
            await asyncio.wait_for(self._ready_event.wait(), timeout=ready_timeout_seconds)
        except Exception:
            await self.aclose()
            raise RuntimeError("PostgreSQL auth invalidation listener did not become ready") from None

    async def _listen_loop(self) -> None:
        while not self._closed:
            try:
                if self._ready_event is not None:
                    self._ready_event.clear()
                self.health.connected = False
                self.health.reconnecting = True
                async with await psycopg.AsyncConnection.connect(self._dsn, autocommit=True) as connection:
                    await connection.execute(f"LISTEN {CHANNEL}")
                    # Reconcile only after LISTEN is registered. A concurrent
                    # auth write is then either visible in this snapshot or
                    # queued as a NOTIFY on this connection.
                    await asyncio.to_thread(self.reconcile_once)
                    self.health.reconnecting = False
                    self.health.last_error_code = None
                    self.health.connected = True
                    if self._ready_event is not None:
                        self._ready_event.set()
                    while not self._closed:
                        async for notify in connection.notifies(timeout=1.0, stop_after=1):
                            if self._closed:
                                break
                            try:
                                payload = json.loads(notify.payload)
                                event = validate_auth_generation_changed(payload)
                            except (TypeError, ValueError):
                                self.health.last_error_code = "invalid_notify_payload"
                                continue
                            self._cache.apply(event.username, event.auth_generation, updated_at=event.changed_at)
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


def auth_generation_notify_sql(event: AuthGenerationChanged) -> tuple[str, dict[str, Any]]:
    validated = validate_auth_generation_changed(event.public_payload())
    payload = json.dumps(validated.public_payload(), ensure_ascii=False, sort_keys=True)
    return f"SELECT pg_notify('{CHANNEL}', :payload)", {"payload": payload}


def _psycopg_conninfo_from_engine(engine: Engine) -> str:
    url = engine.url
    if url.drivername.startswith("postgresql+"):
        url = url.set(drivername="postgresql")
    conninfo = url.render_as_string(hide_password=False)
    conninfo_to_dict(conninfo)
    return conninfo
