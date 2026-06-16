from __future__ import annotations

import unittest

from src.state.contracts import StateHealthSnapshot
from src.state.observability import StatePlatformTelemetry, build_readiness_payload


class StatePlatformHealthTest(unittest.TestCase):
    def test_readiness_payload_contains_required_fields_and_redacts_sensitive_values(self) -> None:
        health = StateHealthSnapshot(
            db_status="ok",
            migration_status="not_ready",
            queue_backlog=9,
            oldest_pending_age_seconds=42.0,
            dead_letter_count=2,
            worker_heartbeat_status="stale",
            ready=False,
            degraded_reason="dsn=<fixture-dsn> token=<fixture>",
        )
        payload = build_readiness_payload(health)
        for key in ("db_status", "migration_status", "queue_backlog", "oldest_pending_age_seconds", "dead_letter_count", "worker_heartbeat_status", "ready", "degraded_reason"):
            self.assertIn(key, payload)
        self.assertNotIn("user:pass", repr(payload))
        self.assertNotIn("token=<fixture>", repr(payload))

    def test_telemetry_payload_does_not_include_raw_payload_or_secrets(self) -> None:
        telemetry = StatePlatformTelemetry(
            operation="enqueue",
            status="retry",
            duration_ms=12.5,
            error_code="40P01",
            partition_category="task",
            attempt_count=2,
            metadata={"payload": {"raw": "prompt"}, "dsn": "postgresql_fixture_dsn"},
        )
        public = telemetry.public_dict()
        self.assertNotIn("prompt", repr(public))
        self.assertNotIn("postgresql://", repr(public))
        self.assertIn("payload_redacted", public["metadata"])

    def test_nested_telemetry_metadata_is_recursively_redacted(self) -> None:
        telemetry = StatePlatformTelemetry(
            operation="enqueue",
            status="retry",
            duration_ms=1.0,
            metadata={"outer": {"dsn": "postgresql_fixture_dsn", "items": [{"token": "nested-token"}]}},
        )
        public = telemetry.public_dict()
        self.assertNotIn("postgresql_fixture_dsn", repr(public))
        self.assertNotIn("nested-token", repr(public))
