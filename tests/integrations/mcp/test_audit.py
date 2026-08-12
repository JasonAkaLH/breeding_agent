from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from src.core.enums import EventVisibility
from src.core.models import Conversation, EventRecord, Task
from src.integrations.mcp.audit import MCPAuditService


NOW = datetime(2026, 8, 12, 12, 0, 0)


class _Storage:
    def __init__(self) -> None:
        self.events = []
        self.deleted_batches = [2, 0]

    async def get_task(self, task_id: str):
        return Task(task_id, "conv-a", "msg-a") if task_id == "task-a" else None

    async def get_conversation(self, conversation_id: str):
        return Conversation("conv-a", "alice") if conversation_id == "conv-a" else None

    async def append_mcp_audit_event(self, event):
        self.events.append(event)
        return event

    async def delete_expired_mcp_audit_events(self, *, now, limit):
        del now, limit
        return self.deleted_batches.pop(0)


class _SafetyDetector:
    def __init__(self) -> None:
        self.violations = []
        self.attestations = []

    async def report_violation(self, **kwargs) -> None:
        self.violations.append(kwargs)

    def attest_interval(self, started_at, ended_at) -> None:
        self.attestations.append((started_at, ended_at))


class MCPAuditServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_secret_boundary_blocks_nested_secret_and_records_red_line(
        self,
    ) -> None:
        storage = _Storage()
        detector = _SafetyDetector()
        service = MCPAuditService(
            storage=storage,
            now_fn=lambda: NOW,
            safety_detector=detector,
        )

        with self.assertRaisesRegex(ValueError, "prohibited secret field"):
            await service.record(
                owner_user_id="alice",
                event_type="mcp.tool_call_started",
                safe_payload={"arguments": {"api_key": "not-persisted"}},
            )

        self.assertEqual(storage.events, [])
        self.assertEqual(
            detector.violations,
            [{"reason_code": "secret_payload_rejected", "observed_at": NOW}],
        )

    async def test_observer_persists_only_allowlisted_safe_fields_for_thirty_days(self) -> None:
        storage = _Storage()
        service = MCPAuditService(storage=storage, now_fn=lambda: NOW)

        await service.observe_event(
            EventRecord(
                event_id="event-a",
                conversation_id="conv-a",
                task_id="task-a",
                node_id="node-a",
                event_type="mcp.tool_call_started",
                payload={
                    "safe_call_ref": "call-safe",
                    "server_display_name": "CRM",
                    "arguments": {"token": "secret"},
                    "endpoint_url": "https://secret.invalid/mcp",
                },
                visibility=EventVisibility.FRONTEND,
                created_at=NOW,
            )
        )

        self.assertEqual(len(storage.events), 1)
        saved = storage.events[0]
        self.assertEqual(saved.owner_user_id, "alice")
        self.assertEqual(saved.call_ref, "call-safe")
        self.assertEqual(
            saved.safe_payload,
            {"safe_call_ref": "call-safe", "server_display_name": "CRM"},
        )
        self.assertEqual(saved.expires_at, NOW + timedelta(days=30))

    async def test_non_mcp_events_are_ignored(self) -> None:
        storage = _Storage()
        service = MCPAuditService(storage=storage, now_fn=lambda: NOW)

        await service.observe_event(
            EventRecord(
                event_id="event-a",
                conversation_id="conv-a",
                task_id="task-a",
                event_type="task.started",
                payload={},
                created_at=NOW,
            )
        )

        self.assertEqual(storage.events, [])

    async def test_records_owner_scoped_configuration_audit_without_endpoint_data(self) -> None:
        storage = _Storage()
        service = MCPAuditService(storage=storage, now_fn=lambda: NOW)

        saved = await service.record(
            owner_user_id="alice",
            event_type="mcp.config_updated",
            server_id="server-a",
            safe_payload={
                "status": "accepted",
                "endpoint_url": "https://secret.invalid/mcp",
            },
        )

        self.assertEqual(saved.owner_user_id, "alice")
        self.assertEqual(saved.server_id, "server-a")
        self.assertEqual(saved.safe_payload, {"status": "accepted"})

    async def test_metric_gap_preserves_only_closed_safe_diagnostics(self) -> None:
        storage = _Storage()
        service = MCPAuditService(storage=storage, now_fn=lambda: NOW)

        await service.observe_event(
            EventRecord(
                event_id="metric-gap-a",
                conversation_id="conv-a",
                task_id="task-a",
                node_id="node-a",
                event_type="mcp.rollout_metric_gap",
                payload={
                    "metric_family": "remote_task_terminal",
                    "gap_reason": "recording_failed",
                    "username": "alice",
                    "exception": "token=secret",
                },
                visibility=EventVisibility.AUDIT_ONLY,
                created_at=NOW,
            )
        )

        self.assertEqual(
            storage.events[0].safe_payload,
            {
                "gap_reason": "recording_failed",
                "metric_family": "remote_task_terminal",
            },
        )

    async def test_rollout_events_use_a_dedicated_low_cardinality_allowlist(self) -> None:
        storage = _Storage()
        service = MCPAuditService(storage=storage, now_fn=lambda: NOW)

        await service.observe_event(
            EventRecord(
                event_id="rollout-event-a",
                conversation_id="conv-a",
                task_id="task-a",
                node_id="node-a",
                event_type="mcp.rollout.route_assigned",
                payload={
                    "safe_task_ref": "task-hmac-a",
                    "safe_owner_ref": "owner-hmac-a",
                    "config_version": "rollout-v1",
                    "rollout_mode": "enforce",
                    "real_path": "user_scoped",
                    "shadow_enabled": False,
                    "reason_code": "stable_hash_selected",
                    "server_display_name": "must-not-cross-rollout-boundary",
                    "username": "alice",
                    "endpoint_url": "https://secret.invalid/mcp",
                    "schema": {"secret": True},
                    "result": {"private": True},
                },
                visibility=EventVisibility.AUDIT_ONLY,
                created_at=NOW,
            )
        )

        self.assertEqual(
            storage.events[0].safe_payload,
            {
                "config_version": "rollout-v1",
                "real_path": "user_scoped",
                "reason_code": "stable_hash_selected",
                "rollout_mode": "enforce",
                "safe_owner_ref": "owner-hmac-a",
                "safe_task_ref": "task-hmac-a",
                "shadow_enabled": False,
            },
        )

    async def test_cleanup_returns_deleted_count(self) -> None:
        storage = _Storage()
        service = MCPAuditService(
            storage=storage,
            now_fn=lambda: NOW,
            cleanup_batch_size=2,
        )

        self.assertEqual(await service.cleanup_expired(), 2)
