from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from src.core.models import EventRecord, MCPAuditEvent


MCP_AUDIT_RETENTION_DAYS = 30
MCP_AUDIT_CLEANUP_BATCH_SIZE = 1000
_SAFE_PAYLOAD_FIELDS = frozenset(
    {
        "decision",
        "error_code",
        "heartbeat_at",
        "input_request_count",
        "next_poll_at",
        "queue_position",
        "reason",
        "safe_call_ref",
        "safe_remote_task_ref",
        "safe_task_ref",
        "server_display_name",
        "status",
        "tool_count",
        "tool_display_name",
    }
)


class MCPAuditService:
    """Persists a redacted MCP-only audit ledger with bounded retention."""

    def __init__(
        self,
        *,
        storage: Any,
        now_fn: Callable[[], datetime] | None = None,
        retention_days: int = MCP_AUDIT_RETENTION_DAYS,
        cleanup_batch_size: int = MCP_AUDIT_CLEANUP_BATCH_SIZE,
    ) -> None:
        if retention_days < 1:
            raise ValueError("retention_days must be positive")
        if cleanup_batch_size < 1:
            raise ValueError("cleanup_batch_size must be positive")
        self._storage = storage
        self._now = now_fn or (lambda: datetime.now(timezone.utc).replace(tzinfo=None))
        self._retention = timedelta(days=retention_days)
        self._cleanup_batch_size = cleanup_batch_size

    async def observe_event(self, event: EventRecord) -> None:
        if not event.event_type.startswith("mcp."):
            return
        task = await self._storage.get_task(event.task_id)
        if task is None:
            return
        conversation = await self._storage.get_conversation(task.conversation_id)
        if conversation is None or not conversation.username:
            return
        occurred_at = event.created_at or self._now()
        safe_payload = _safe_payload(event.payload)
        await self.record(
            owner_user_id=conversation.username,
            event_type=event.event_type,
            occurred_at=occurred_at,
            task_id=event.task_id,
            node_id=event.node_id,
            call_ref=_optional_text(safe_payload.get("safe_call_ref")),
            safe_payload=safe_payload,
            source_ref=event.event_id,
        )

    async def record(
        self,
        *,
        owner_user_id: str,
        event_type: str,
        occurred_at: datetime | None = None,
        task_id: str | None = None,
        node_id: str | None = None,
        server_id: str | None = None,
        call_ref: str | None = None,
        safe_payload: Mapping[str, Any] | None = None,
        source_ref: str | None = None,
    ) -> MCPAuditEvent:
        timestamp = occurred_at or self._now()
        return await self._storage.append_mcp_audit_event(
            MCPAuditEvent(
                audit_event_id=_audit_event_id(source_ref or f"{event_type}:{timestamp.isoformat()}"),
                owner_user_id=owner_user_id,
                event_type=event_type,
                occurred_at=timestamp,
                expires_at=timestamp + self._retention,
                task_id=task_id,
                node_id=node_id,
                server_id=server_id,
                call_ref=call_ref,
                safe_payload=_safe_payload(safe_payload or {}),
            )
        )

    async def cleanup_expired(self) -> int:
        deleted = 0
        while True:
            batch = await self._storage.delete_expired_mcp_audit_events(
                now=self._now(),
                limit=self._cleanup_batch_size,
            )
            deleted += batch
            if batch < self._cleanup_batch_size:
                return deleted
            await asyncio.sleep(0)

    async def run_retention_forever(self, *, interval_seconds: float = 3600.0) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        while True:
            await self.cleanup_expired()
            await asyncio.sleep(interval_seconds)


def _safe_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key in _SAFE_PAYLOAD_FIELDS:
        value = payload.get(key)
        if value is None or isinstance(value, bool | int | float):
            if value is not None:
                safe[key] = value
            continue
        if isinstance(value, str):
            safe[key] = " ".join(value.split())[:500]
    return safe


def _audit_event_id(event_id: str) -> str:
    digest = hashlib.sha256(event_id.encode("utf-8")).hexdigest()[:32]
    return f"mcp-audit-{digest}"


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


__all__ = ["MCPAuditService", "MCP_AUDIT_RETENTION_DAYS"]
