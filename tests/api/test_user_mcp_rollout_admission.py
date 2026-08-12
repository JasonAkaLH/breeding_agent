from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

from src.api.dto import SubmitMessageRequest
from src.api.runtime import ApiRuntime, _MCPRolloutInstanceAdmission


class _Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 8, 13, 8, 0, 0)

    def advance(self, *, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


class _LeaseStorage:
    def __init__(self, *, fail_after: int | None = None) -> None:
        self.fail_after = fail_after
        self.leases = []

    async def save_mcp_rollout_instance_config_lease(self, lease):
        if self.fail_after is not None and len(self.leases) >= self.fail_after:
            raise RuntimeError("secret-token-must-not-be-observed")
        self.leases.append(lease)
        return lease


class _AuditSink:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, object]]] = []

    def record_sync(self, event_type, payload, **_kwargs) -> None:
        self.records.append((event_type, dict(payload)))


def _runtime(
    clock: _Clock,
    storage: _LeaseStorage,
    audit_sink: _AuditSink,
) -> ApiRuntime:
    runtime = object.__new__(ApiRuntime)
    runtime.storage = storage
    runtime.mcp_rollout_config = SimpleNamespace(fingerprint="config-fingerprint")
    runtime._mcp_rollout_instance_admission = _MCPRolloutInstanceAdmission(
        environment_id="production",
        deployment_id="deployment-1",
        stage="cohort_enforce",
        activation_id="activation-1",
        instance_id="instance-1",
    )
    runtime._mcp_rollout_instance_lease_created_at = None
    runtime._mcp_rollout_instance_lease_valid_until = None
    runtime._mcp_rollout_instance_admission_error = None
    runtime._mcp_rollout_instance_lease_task = None
    runtime._mcp_rollout_lease_duration_seconds = 60
    runtime._mcp_rollout_lease_renew_interval_seconds = 20
    runtime._audit_sink = audit_sink
    runtime._utcnow_naive = lambda: clock.now
    return runtime


class UserMCPRolloutAdmissionTest(unittest.IsolatedAsyncioTestCase):
    async def test_renewal_preserves_initial_created_at(self) -> None:
        clock = _Clock()
        storage = _LeaseStorage()
        runtime = _runtime(clock, storage, _AuditSink())

        await runtime._admit_mcp_rollout_instance()
        clock.advance(seconds=20)
        await runtime._save_mcp_rollout_instance_lease(now=clock.now)
        await runtime._stop_mcp_rollout_instance_lease_renewal()

        self.assertEqual(len(storage.leases), 2)
        first, renewed = storage.leases
        self.assertEqual(renewed.created_at, first.created_at)
        self.assertEqual(renewed.updated_at, first.updated_at + timedelta(seconds=20))
        self.assertEqual(
            renewed.lease_expires_at,
            renewed.updated_at + timedelta(seconds=60),
        )

    async def test_initial_admission_failure_prevents_renewal_start(self) -> None:
        clock = _Clock()
        storage = _LeaseStorage(fail_after=0)
        runtime = _runtime(clock, storage, _AuditSink())

        with self.assertRaisesRegex(RuntimeError, "secret-token"):
            await runtime._admit_mcp_rollout_instance()

        self.assertIsNone(runtime._mcp_rollout_instance_lease_task)
        self.assertIsNone(runtime._mcp_rollout_instance_lease_valid_until)

    async def test_renewal_failure_rejects_new_submission_without_error_leak(self) -> None:
        clock = _Clock()
        storage = _LeaseStorage(fail_after=1)
        audit_sink = _AuditSink()
        runtime = _runtime(clock, storage, audit_sink)
        runtime._mcp_rollout_lease_renew_interval_seconds = 0

        await runtime._admit_mcp_rollout_instance()
        await runtime._mcp_rollout_instance_lease_task

        with self.assertRaisesRegex(
            RuntimeError,
            "mcp_rollout_instance_admission_lost",
        ):
            await runtime.submit_message(
                "conversation-1",
                SubmitMessageRequest(
                    conversation_id="conversation-1",
                    content="hello",
                ),
                authenticated_username="sensitive-user-name",
            )

        self.assertEqual(runtime._mcp_rollout_instance_admission_error, "lease_renewal_failed")
        rendered = repr(audit_sink.records)
        self.assertNotIn("secret-token", rendered)
        self.assertNotIn("sensitive-user-name", rendered)
        self.assertIn("lease_renewal_failed", rendered)

    async def test_expired_lease_rejects_new_submission(self) -> None:
        clock = _Clock()
        audit_sink = _AuditSink()
        runtime = _runtime(clock, _LeaseStorage(), audit_sink)

        await runtime._admit_mcp_rollout_instance()
        await runtime._stop_mcp_rollout_instance_lease_renewal()
        clock.advance(seconds=60)

        with self.assertRaisesRegex(
            RuntimeError,
            "mcp_rollout_instance_admission_lost",
        ):
            await runtime.submit_message(
                "conversation-1",
                SubmitMessageRequest(
                    conversation_id="conversation-1",
                    content="hello",
                ),
                authenticated_username="sensitive-user-name",
            )

        self.assertEqual(runtime._mcp_rollout_instance_admission_error, "lease_expired")
        self.assertIn("lease_expired", repr(audit_sink.records))
        self.assertNotIn("sensitive-user-name", repr(audit_sink.records))

    async def test_stop_cancels_renewal_task(self) -> None:
        clock = _Clock()
        runtime = _runtime(clock, _LeaseStorage(), _AuditSink())

        await runtime._admit_mcp_rollout_instance()
        renewal_task = runtime._mcp_rollout_instance_lease_task
        await runtime._stop_mcp_rollout_instance_lease_renewal()

        self.assertIsNotNone(renewal_task)
        self.assertTrue(renewal_task.cancelled())
        self.assertIsNone(runtime._mcp_rollout_instance_lease_task)


if __name__ == "__main__":
    unittest.main()
