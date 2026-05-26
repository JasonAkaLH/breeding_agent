from __future__ import annotations

import unittest

from src.state.contracts import CommandStatus
from src.state.postgres.schema import (
    COMMAND_QUEUE_TABLE,
    POSTGRES_STATE_TABLES,
    STATE_WRITE_COMMAND_COLUMNS,
    build_schema_ddl,
)


class PostgresStateSchemaContractTest(unittest.TestCase):
    def test_schema_contains_required_tables_columns_indexes_and_constraints(self) -> None:
        self.assertIn("state_write_command", POSTGRES_STATE_TABLES)
        self.assertIn("state_partition_cursor", POSTGRES_STATE_TABLES)
        self.assertIn("state_write_dead_letter", POSTGRES_STATE_TABLES)
        self.assertIn("state_migration_ledger", POSTGRES_STATE_TABLES)
        for column in (
            "command_id",
            "command_type",
            "idempotency_key",
            "payload_fingerprint",
            "partition_key",
            "partition_sequence",
            "payload",
            "status",
            "priority",
            "available_at",
            "attempt_count",
            "max_attempts",
            "lease_owner",
            "lease_expires_at",
            "last_error_code",
            "last_error_message",
            "result",
            "created_at",
            "updated_at",
            "completed_at",
        ):
            self.assertIn(column, STATE_WRITE_COMMAND_COLUMNS)
        self.assertIn("uq_state_write_command_type_idempotency", COMMAND_QUEUE_TABLE.unique_constraints)
        self.assertIn("uq_state_write_partition_sequence", COMMAND_QUEUE_TABLE.unique_constraints)
        self.assertIn("ix_state_write_claim_ready", COMMAND_QUEUE_TABLE.indexes)
        self.assertIn("ix_state_write_partition_outstanding", COMMAND_QUEUE_TABLE.indexes)

    def test_schema_ddl_is_pure_ddl_and_archive_matches_descriptor(self) -> None:
        ddl = build_schema_ddl()
        archive = POSTGRES_STATE_TABLES["state_write_archive"]
        for column in archive.columns:
            self.assertIn(column, ddl)
        self.assertNotIn("FOR UPDATE SKIP LOCKED", ddl)
        self.assertNotIn(":worker_id", ddl)
        self.assertNotIn("UPDATE state_write_command", ddl)

    def test_schema_ddl_declares_status_enum_and_no_plaintext_secret_columns(self) -> None:
        ddl = build_schema_ddl()
        for status in CommandStatus:
            self.assertIn(status.value, ddl)
        self.assertNotIn("password", ddl.lower())
        self.assertNotIn("dsn", ddl.lower())
