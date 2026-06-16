from __future__ import annotations

import inspect
import unittest
from datetime import datetime, timedelta, timezone

from src.state.commands import build_command, command_partition_key, payload_fingerprint
from src.state.contracts import (
    CommandStatus,
    StateCommand,
    StateCommandRecord,
    StateCommandResult,
    StateHealthSnapshot,
    StateReadStore,
    StateService,
    StateWriteQueue,
)


class StatePlatformContractTest(unittest.TestCase):
    def test_contracts_expose_read_write_queue_and_service_boundaries(self) -> None:
        self.assertTrue(hasattr(StateReadStore, "get_conversation"))
        self.assertTrue(hasattr(StateReadStore, "list_messages_for_conversation"))
        self.assertTrue(hasattr(StateWriteQueue, "enqueue"))
        self.assertTrue(hasattr(StateWriteQueue, "claim_next"))
        self.assertTrue(hasattr(StateWriteQueue, "complete"))
        self.assertTrue(hasattr(StateService, "query"))
        self.assertTrue(hasattr(StateService, "submit_command"))
        self.assertTrue(hasattr(StateService, "execute_command_and_wait"))
        self.assertTrue(hasattr(StateService, "transactional_command_group"))
        self.assertTrue(inspect.iscoroutinefunction(StateWriteQueue.enqueue))
        self.assertTrue(inspect.iscoroutinefunction(StateService.execute_command_and_wait))

    def test_state_command_record_contains_durable_queue_fields_and_safe_dump(self) -> None:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        command = StateCommand(
            command_id="cmd-1",
            command_type="conversation.create",
            idempotency_key="idem-1",
            payload_fingerprint="sha256:abc",
            partition_key="conversation:conv-1",
            payload={"conversation_id": "conv-1", "token": "<fixture>", "dsn": "postgres_fixture_dsn"},
            created_at=now,
        )
        record = StateCommandRecord(
            command=command,
            status=CommandStatus.LEASED,
            partition_sequence=7,
            priority=2,
            available_at=now,
            attempt_count=1,
            max_attempts=3,
            lease_owner="worker-a",
            lease_expires_at=now + timedelta(seconds=30),
            updated_at=now,
            last_error_code="40P01",
            last_error_message="deadlock detected",
        )

        self.assertEqual(record.command.command_id, "cmd-1")
        self.assertEqual(record.partition_sequence, 7)
        self.assertEqual(record.status, CommandStatus.LEASED)
        public = record.public_dict()
        dumped = repr(public)
        record_repr = repr(record)
        self.assertIn("payload_redacted", public)
        self.assertNotIn("<fixture>", record_repr)
        self.assertNotIn("postgres_fixture_dsn", record_repr)
        self.assertNotIn("<fixture>", dumped)
        self.assertNotIn("postgres://", dumped)
        self.assertNotIn("u:p", dumped)

    def test_payload_fingerprint_is_stable_and_partition_rules_are_explicit(self) -> None:
        first = payload_fingerprint({"b": 2, "a": 1})
        second = payload_fingerprint({"a": 1, "b": 2})
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("sha256:"))
        self.assertEqual(command_partition_key("conversation.create", {"conversation_id": "c1"}), "conversation:c1")
        self.assertEqual(command_partition_key("task.update", {"task_id": "t1"}), "task:t1")
        self.assertEqual(command_partition_key("auth.rotate_token", {"username": "alice"}), "auth:alice")
        command = build_command(
            command_type="conversation.create",
            idempotency_key="idem",
            payload={"conversation_id": "c1"},
            command_id="cmd",
            created_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        self.assertEqual(command.partition_key, "conversation:c1")

    def test_health_snapshot_covers_readiness_dependencies_and_redacts_details(self) -> None:
        health = StateHealthSnapshot(
            db_status="ok",
            migration_status="not_ready",
            queue_backlog=4,
            oldest_pending_age_seconds=12.5,
            dead_letter_count=1,
            worker_heartbeat_status="stale",
            ready=False,
            degraded_reason="migration_not_ready: <fixture-dsn>",
        )
        public = health.public_dict()
        self.assertFalse(public["ready"])
        for key in (
            "db_status",
            "migration_status",
            "queue_backlog",
            "oldest_pending_age_seconds",
            "dead_letter_count",
            "worker_heartbeat_status",
            "degraded_reason",
        ):
            self.assertIn(key, public)
        self.assertNotIn("secret", repr(public))
        self.assertNotIn("postgresql://", repr(public))

    def test_nested_command_result_metadata_is_recursively_redacted(self) -> None:
        result = StateCommandResult(
            command_id="cmd",
            status=CommandStatus.SUCCEEDED,
            result={"nested": {"dsn": "postgresql_fixture_dsn", "items": [{"token": "nested-token"}]}},
        )
        public = result.public_dict()
        self.assertNotIn("postgresql_fixture_dsn", repr(public))
        self.assertNotIn("nested-token", repr(public))


if __name__ == "__main__":
    unittest.main()
