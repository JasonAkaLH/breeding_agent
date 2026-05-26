from __future__ import annotations

from dataclasses import dataclass

from src.state.contracts import CommandStatus


@dataclass(frozen=True, slots=True)
class TableDescriptor:
    name: str
    columns: tuple[str, ...]
    indexes: tuple[str, ...] = ()
    unique_constraints: tuple[str, ...] = ()


STATE_WRITE_COMMAND_COLUMNS = (
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
)

COMMAND_QUEUE_TABLE = TableDescriptor(
    name="state_write_command",
    columns=STATE_WRITE_COMMAND_COLUMNS,
    unique_constraints=(
        "uq_state_write_command_type_idempotency",
        "uq_state_write_partition_sequence",
    ),
    indexes=(
        "ix_state_write_claim_ready",
        "ix_state_write_partition_outstanding",
    ),
)

POSTGRES_STATE_TABLES = {
    COMMAND_QUEUE_TABLE.name: COMMAND_QUEUE_TABLE,
    "state_partition_cursor": TableDescriptor(
        name="state_partition_cursor",
        columns=("partition_key", "next_sequence", "updated_at"),
    ),
    "state_write_dead_letter": TableDescriptor(
        name="state_write_dead_letter",
        columns=("dead_letter_id", *STATE_WRITE_COMMAND_COLUMNS, "dead_lettered_at"),
    ),
    "state_write_archive": TableDescriptor(
        name="state_write_archive",
        columns=("archived_at", *STATE_WRITE_COMMAND_COLUMNS),
    ),
    "state_migration_ledger": TableDescriptor(
        name="state_migration_ledger",
        columns=("migration_id", "schema_version", "status", "checksum", "started_at", "finished_at", "metadata"),
    ),
}


def build_schema_ddl(*, guarded: bool = False) -> str:
    status_values = ", ".join(f"'{status.value}'" for status in CommandStatus)
    type_ddl = f"CREATE TYPE state_command_status AS ENUM ({status_values});"
    if guarded:
        type_ddl = f"""DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'state_command_status') THEN
        CREATE TYPE state_command_status AS ENUM ({status_values});
    END IF;
END
$$;"""
    return f"""
{type_ddl}

CREATE TABLE IF NOT EXISTS state_write_command (
    command_id text PRIMARY KEY,
    command_type text NOT NULL,
    idempotency_key text NOT NULL,
    payload_fingerprint text NOT NULL,
    partition_key text NOT NULL,
    partition_sequence bigint NOT NULL,
    payload jsonb NOT NULL,
    status state_command_status NOT NULL,
    priority integer NOT NULL DEFAULT 0,
    available_at timestamptz NOT NULL,
    attempt_count integer NOT NULL DEFAULT 0,
    max_attempts integer NOT NULL DEFAULT 3,
    lease_owner text,
    lease_expires_at timestamptz,
    last_error_code text,
    last_error_message text,
    result jsonb,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    completed_at timestamptz,
    CONSTRAINT uq_state_write_command_type_idempotency UNIQUE (command_type, idempotency_key),
    CONSTRAINT uq_state_write_partition_sequence UNIQUE (partition_key, partition_sequence)
);
CREATE INDEX IF NOT EXISTS ix_state_write_claim_ready ON state_write_command (status, available_at, priority DESC, created_at);
CREATE INDEX IF NOT EXISTS ix_state_write_partition_outstanding ON state_write_command (partition_key, partition_sequence) WHERE status NOT IN ('succeeded', 'failed', 'dead_lettered', 'cancelled');

CREATE TABLE IF NOT EXISTS state_partition_cursor (
    partition_key text PRIMARY KEY,
    next_sequence bigint NOT NULL,
    updated_at timestamptz NOT NULL
);
CREATE TABLE IF NOT EXISTS state_write_dead_letter (
    dead_letter_id text PRIMARY KEY,
    command_id text NOT NULL,
    command_type text NOT NULL,
    idempotency_key text NOT NULL,
    payload_fingerprint text NOT NULL,
    partition_key text NOT NULL,
    partition_sequence bigint NOT NULL,
    payload jsonb NOT NULL,
    status state_command_status NOT NULL,
    priority integer NOT NULL DEFAULT 0,
    available_at timestamptz NOT NULL,
    attempt_count integer NOT NULL,
    max_attempts integer NOT NULL,
    lease_owner text,
    lease_expires_at timestamptz,
    last_error_code text,
    last_error_message text,
    result jsonb,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    completed_at timestamptz,
    dead_lettered_at timestamptz NOT NULL
);
CREATE TABLE IF NOT EXISTS state_write_archive (
    archived_at timestamptz NOT NULL,
    command_id text NOT NULL,
    command_type text NOT NULL,
    idempotency_key text NOT NULL,
    payload_fingerprint text NOT NULL,
    partition_key text NOT NULL,
    partition_sequence bigint NOT NULL,
    payload jsonb NOT NULL,
    status state_command_status NOT NULL,
    priority integer NOT NULL DEFAULT 0,
    available_at timestamptz NOT NULL,
    attempt_count integer NOT NULL,
    max_attempts integer NOT NULL,
    lease_owner text,
    lease_expires_at timestamptz,
    last_error_code text,
    last_error_message text,
    result jsonb,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    completed_at timestamptz
);
CREATE TABLE IF NOT EXISTS state_migration_ledger (
    migration_id text PRIMARY KEY,
    schema_version text NOT NULL,
    status text NOT NULL,
    checksum text,
    started_at timestamptz NOT NULL,
    finished_at timestamptz,
    metadata jsonb NOT NULL
);
""".strip()
