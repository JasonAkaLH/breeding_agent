from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from src.core.models import EventRecord, MCPAuditEvent, MCPShadowAuditSample

from .shadow_evidence import validate_shadow_audit_sample
from .safety_detectors import AuthoritativeMCPSafetyDetector
from .observability import (
    validate_mcp_aggregate_transition_payload,
)


MCP_AUDIT_RETENTION_DAYS = 30
MCP_AUDIT_CLEANUP_BATCH_SIZE = 1000
MCP_AGGREGATE_TRANSITION_EVENT_TYPE = "mcp.aggregate_transition"
MCP_ROLLOUT_AUDIT_EVENT_TYPES = frozenset(
    {
        "mcp.rollout.route_assigned",
        "mcp.rollout.shadow_compared",
        "mcp.rollout.mode_changed",
        "mcp.rollout.rollback_triggered",
        "mcp.legacy.config_migrated",
        "mcp.legacy.runtime_disabled",
        "mcp.legacy.runtime_removed",
    }
)
_SAFE_PAYLOAD_FIELDS = frozenset(
    {
        "artifact_count",
        "decision",
        "error_code",
        "heartbeat_at",
        "gap_reason",
        "input_request_count",
        "plaintext_http",
        "credential_over_plaintext_http",
        "metric_family",
        "next_poll_at",
        "queue_position",
        "reason",
        "reason_code",
        "safe_call_ref",
        "safe_server_ref",
        "safe_remote_task_ref",
        "safe_task_ref",
        "server_display_name",
        "schema",
        "status",
        "binding_mode",
        "selector_action",
        "tool_call_dispatched",
        "tool_count",
        "tool_display_name",
    }
)
_ROLLOUT_SAFE_PAYLOAD_FIELDS = frozenset(
    {
        "cohort_id",
        "config_fingerprint",
        "config_version",
        "diff_category",
        "reason_code",
        "real_path",
        "result_category",
        "rollout_mode",
        "safe_owner_ref",
        "safe_task_ref",
        "shadow_enabled",
        "stage",
    }
)
_SECRET_FIELD_MARKERS = (
    "authorization",
    "cookie",
    "credential",
    "api_key",
    "apikey",
    "nonce",
    "password",
    "secret",
    "token",
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
        safety_detector: AuthoritativeMCPSafetyDetector | None = None,
    ) -> None:
        if retention_days < 1:
            raise ValueError("retention_days must be positive")
        if cleanup_batch_size < 1:
            raise ValueError("cleanup_batch_size must be positive")
        self._storage = storage
        self._now = now_fn or (lambda: datetime.now(timezone.utc).replace(tzinfo=None))
        self._retention = timedelta(days=retention_days)
        self._cleanup_batch_size = cleanup_batch_size
        self._safety_detector = safety_detector

    def configure_safety_detector(
        self, detector: AuthoritativeMCPSafetyDetector
    ) -> None:
        self._safety_detector = detector

    def attest_safety_interval(
        self, bucket_started_at: datetime, bucket_ended_at: datetime
    ) -> None:
        if self._safety_detector is None:
            raise RuntimeError("MCP audit safety detector is not configured")
        self._safety_detector.attest_interval(bucket_started_at, bucket_ended_at)

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
        await self._reject_secret_payload(event.payload, occurred_at=occurred_at)
        if event.event_type == MCP_AGGREGATE_TRANSITION_EVENT_TYPE:
            validate_mcp_aggregate_transition_payload(event.payload)
        safe_payload = _safe_payload(event.payload, event_type=event.event_type)
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
        await self._reject_secret_payload(safe_payload or {}, occurred_at=timestamp)
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
                safe_payload=_safe_payload(
                    safe_payload or {}, event_type=event_type
                ),
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
                break
            await asyncio.sleep(0)
        delete_shadow = getattr(
            self._storage, "delete_expired_mcp_shadow_audit_samples", None
        )
        if delete_shadow is None:
            return deleted
        while True:
            batch = await delete_shadow(
                now=self._now(), limit=self._cleanup_batch_size
            )
            deleted += batch
            if batch < self._cleanup_batch_size:
                return deleted
            await asyncio.sleep(0)

    async def record_shadow_sample(
        self, sample: MCPShadowAuditSample
    ) -> MCPShadowAuditSample:
        blockers = validate_shadow_audit_sample(sample)
        if blockers:
            raise ValueError(f"MCP shadow audit sample is invalid: {','.join(blockers)}")
        return await self._storage.save_mcp_shadow_audit_sample(sample)

    async def _reject_secret_payload(
        self, payload: Mapping[str, Any], *, occurred_at: datetime
    ) -> None:
        if self._safety_detector is None or not _contains_secret_field(payload):
            return
        await self._safety_detector.report_violation(
            reason_code="secret_payload_rejected",
            observed_at=occurred_at,
        )
        raise ValueError("MCP audit payload contains a prohibited secret field")

    async def run_retention_forever(self, *, interval_seconds: float = 3600.0) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        while True:
            await self.cleanup_expired()
            await asyncio.sleep(interval_seconds)


def _safe_payload(
    payload: Mapping[str, Any], *, event_type: str | None = None
) -> dict[str, Any]:
    allowed_fields = _SAFE_PAYLOAD_FIELDS
    if event_type == MCP_AGGREGATE_TRANSITION_EVENT_TYPE:
        transition = validate_mcp_aggregate_transition_payload(payload)
        return transition.as_payload()
    if event_type in MCP_ROLLOUT_AUDIT_EVENT_TYPES:
        allowed_fields = _ROLLOUT_SAFE_PAYLOAD_FIELDS
    safe: dict[str, Any] = {}
    for key in allowed_fields:
        value = payload.get(key)
        if value is None or isinstance(value, bool | int | float):
            if value is not None:
                safe[key] = value
            continue
        if isinstance(value, str):
            safe[key] = " ".join(value.split())[:500]
    return safe


def _contains_secret_field(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(
            (
                any(marker in str(key).lower() for marker in _SECRET_FIELD_MARKERS)
                and not (
                    str(key) == "credential_over_plaintext_http"
                    and isinstance(item, bool)
                )
            )
            or _contains_secret_field(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_secret_field(item) for item in value)
    return False


def _audit_event_id(event_id: str) -> str:
    digest = hashlib.sha256(event_id.encode("utf-8")).hexdigest()[:32]
    return f"mcp-audit-{digest}"


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


__all__ = [
    "MCPAuditService",
    "MCP_AUDIT_RETENTION_DAYS",
    "MCP_ROLLOUT_AUDIT_EVENT_TYPES",
    "MCP_AGGREGATE_TRANSITION_EVENT_TYPE",
]
