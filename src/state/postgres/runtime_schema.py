from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Mapping

from sqlalchemy import CheckConstraint
from sqlalchemy.schema import CreateIndex, CreateTable
from sqlalchemy.dialects import postgresql

# Importing models registers all runtime tables on SQLiteBase.metadata.
import src.storage.sqlite.models  # noqa: F401
from src.storage.sqlite.base import SQLiteBase

from .schema import POSTGRES_STATE_TABLES, build_schema_ddl

POSTGRES_RUNTIME_SCHEMA_VERSION = "maf.postgresql_fresh_runtime_schema.v8"
POSTGRES_RUNTIME_TABLES = tuple(sorted(SQLiteBase.metadata.tables))
POSTGRES_APPEND_ONLY_TABLES = (
    "mcp_cp7_ready_epoch_event",
    "mcp_cp7_safety_ledger",
    "mcp_legacy_retirement_evidence",
    "mcp_legacy_retirement_receipt",
    "mcp_no_server_convergence_receipt",
    "mcp_terminal_result_receipt",
)
POSTGRES_CP7_TRIGGER_NAMES = (
    "trg_mcp_execution_terminal_projection_monotonic",
    "trg_mcp_execution_terminal_projection_reject_delete",
    "trg_mcp_cp7_candidate_guard_monotonic",
    "trg_mcp_cp7_candidate_guard_reject_delete",
    "trg_mcp_cp7_ready_epoch_event_append_only",
    "trg_mcp_cp7_safety_attestation_window",
    "trg_mcp_cp7_safety_ledger_append_only",
    "trg_mcp_terminal_result_receipt_append_only",
    "trg_user_mcp_owner_guard_monotonic",
    "trg_user_mcp_owner_guard_reject_delete",
)


@dataclass(frozen=True, slots=True)
class PostgresFreshCutoverSchemaManifest:
    schema_version: str
    runtime_table_names: tuple[str, ...]
    operational_table_names: tuple[str, ...]
    table_columns: Mapping[str, Mapping[str, str]]
    check_constraints: Mapping[str, Mapping[str, str]]
    append_only_tables: tuple[str, ...]
    trigger_names: tuple[str, ...]
    checksum: str

    def with_runtime_table_names(self, names: tuple[str, ...]) -> "PostgresFreshCutoverSchemaManifest":
        columns = dict(self.table_columns)
        payload = _manifest_payload(
            schema_version=self.schema_version,
            runtime_table_names=names,
            operational_table_names=self.operational_table_names,
            table_columns=columns,
            check_constraints=self.check_constraints,
            append_only_tables=self.append_only_tables,
            trigger_names=self.trigger_names,
        )
        return PostgresFreshCutoverSchemaManifest(
            schema_version=self.schema_version,
            runtime_table_names=names,
            operational_table_names=self.operational_table_names,
            table_columns=columns,
            check_constraints=self.check_constraints,
            append_only_tables=self.append_only_tables,
            trigger_names=self.trigger_names,
            checksum=_checksum(payload),
        )


def build_postgres_fresh_cutover_schema_manifest() -> PostgresFreshCutoverSchemaManifest:
    table_columns: dict[str, dict[str, str]] = {}
    check_constraints: dict[str, dict[str, str]] = {}
    for table_name, table in sorted(SQLiteBase.metadata.tables.items()):
        table_columns[table_name] = {column.name: _postgres_type_name(column.type) for column in table.columns}
        check_constraints[table_name] = {
            str(constraint.name): str(constraint.sqltext)
            for constraint in sorted(
                (item for item in table.constraints if isinstance(item, CheckConstraint)),
                key=lambda item: str(item.name),
            )
        }
    operational = tuple(sorted(POSTGRES_STATE_TABLES))
    for table_name in operational:
        descriptor = POSTGRES_STATE_TABLES[table_name]
        table_columns[table_name] = {column: _state_column_type(column) for column in descriptor.columns}
    payload = _manifest_payload(
        schema_version=POSTGRES_RUNTIME_SCHEMA_VERSION,
        runtime_table_names=POSTGRES_RUNTIME_TABLES,
        operational_table_names=operational,
        table_columns=table_columns,
        check_constraints=check_constraints,
        append_only_tables=POSTGRES_APPEND_ONLY_TABLES,
        trigger_names=POSTGRES_CP7_TRIGGER_NAMES,
    )
    return PostgresFreshCutoverSchemaManifest(
        schema_version=POSTGRES_RUNTIME_SCHEMA_VERSION,
        runtime_table_names=POSTGRES_RUNTIME_TABLES,
        operational_table_names=operational,
        table_columns=table_columns,
        check_constraints=check_constraints,
        append_only_tables=POSTGRES_APPEND_ONLY_TABLES,
        trigger_names=POSTGRES_CP7_TRIGGER_NAMES,
        checksum=_checksum(payload),
    )


def build_runtime_table_schema_ddl() -> str:
    dialect = postgresql.dialect()
    statements: list[str] = []
    for table in SQLiteBase.metadata.sorted_tables:
        statements.append(str(CreateTable(table, if_not_exists=True).compile(dialect=dialect)).strip() + ";")
    return "\n".join(statements)


def build_runtime_index_schema_ddl() -> str:
    dialect = postgresql.dialect()
    statements: list[str] = []
    for table in SQLiteBase.metadata.sorted_tables:
        for index in table.indexes:
            statements.append(str(CreateIndex(index, if_not_exists=True).compile(dialect=dialect)).strip() + ";")
    return "\n".join(statements)


def build_runtime_schema_ddl() -> str:
    return "\n".join(
        (
            build_runtime_table_schema_ddl(),
            build_runtime_index_schema_ddl(),
            build_runtime_mutation_trigger_schema_ddl(),
        )
    )


def build_runtime_mutation_trigger_schema_ddl() -> str:
    statements = [
        """CREATE OR REPLACE FUNCTION maf_reject_append_only_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'cp7_append_only_violation';
END
$$;""",
        """CREATE OR REPLACE FUNCTION maf_enforce_cp7_candidate_guard_monotonic()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.invalid_latched = true THEN
        IF NEW.invalid_latched <> true
           OR NEW.first_invalid_record_id IS DISTINCT FROM OLD.first_invalid_record_id
           OR NEW.first_invalid_reason IS DISTINCT FROM OLD.first_invalid_reason
           OR NEW.first_invalid_at IS DISTINCT FROM OLD.first_invalid_at THEN
            RAISE EXCEPTION 'cp7_candidate_guard_monotonic';
        END IF;
    ELSIF NEW.invalid_latched = true THEN
        IF NEW.first_invalid_record_id IS NULL OR NEW.first_invalid_reason IS NULL
           OR NEW.first_invalid_at IS NULL THEN
            RAISE EXCEPTION 'cp7_candidate_guard_monotonic';
        END IF;
    ELSIF NEW.first_invalid_record_id IS NOT NULL OR NEW.first_invalid_reason IS NOT NULL
          OR NEW.first_invalid_at IS NOT NULL THEN
        RAISE EXCEPTION 'cp7_candidate_guard_monotonic';
    END IF;
    IF NEW.candidate_id IS DISTINCT FROM OLD.candidate_id
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'cp7_candidate_guard_identity';
    END IF;
    RETURN NEW;
END
$$;""",
        """CREATE OR REPLACE FUNCTION maf_enforce_user_mcp_owner_guard_monotonic()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.owner_user_id IS DISTINCT FROM OLD.owner_user_id
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
       OR NEW.revision <> OLD.revision + 1 THEN
        RAISE EXCEPTION 'user_mcp_owner_guard_monotonic';
    END IF;
    RETURN NEW;
END
$$;""",
        """CREATE OR REPLACE FUNCTION maf_enforce_mcp_terminal_projection_monotonic()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT (
        OLD.status = 'unknown' AND OLD.revision = 0
        AND NEW.status = 'late_result_resolved' AND NEW.revision = 1
        AND NEW.projection_id IS NOT DISTINCT FROM OLD.projection_id
        AND NEW.owner_user_id IS NOT DISTINCT FROM OLD.owner_user_id
        AND NEW.conversation_id IS NOT DISTINCT FROM OLD.conversation_id
        AND NEW.intent_id IS NOT DISTINCT FROM OLD.intent_id
        AND NEW.call_id IS NOT DISTINCT FROM OLD.call_id
        AND NEW.task_id IS NOT DISTINCT FROM OLD.task_id
        AND NEW.node_id IS NOT DISTINCT FROM OLD.node_id
        AND NEW.unknown_event_id IS NOT DISTINCT FROM OLD.unknown_event_id
        AND NEW.task_failed_event_id IS NOT DISTINCT FROM OLD.task_failed_event_id
        AND NEW.unknown_terminal_at IS NOT DISTINCT FROM OLD.unknown_terminal_at
        AND NEW.created_at IS NOT DISTINCT FROM OLD.created_at
    ) THEN
        RAISE EXCEPTION 'mcp_terminal_projection_monotonic';
    END IF;
    RETURN NEW;
END
$$;""",
        """CREATE OR REPLACE FUNCTION maf_validate_cp7_safety_attestation_window()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.record_kind = 'attestation' AND NOT (
        date_trunc('minute', NEW.bucket_started_at) = NEW.bucket_started_at
        AND date_trunc('minute', NEW.bucket_ended_at) = NEW.bucket_ended_at
        AND NEW.bucket_ended_at = NEW.bucket_started_at + interval '60 seconds'
    ) THEN
        RAISE EXCEPTION 'cp7_attestation_window_invalid';
    END IF;
    RETURN NEW;
END
$$;""",
    ]
    for table_name in POSTGRES_APPEND_ONLY_TABLES:
        trigger_name = f"trg_{table_name}_append_only"
        statements.append(
            f"""DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = '{trigger_name}') THEN
        CREATE TRIGGER {trigger_name}
        BEFORE UPDATE OR DELETE ON {table_name}
        FOR EACH ROW EXECUTE FUNCTION maf_reject_append_only_mutation();
    END IF;
END
$$;"""
        )
    statements.extend(
        (
            """DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_mcp_cp7_candidate_guard_monotonic') THEN
        CREATE TRIGGER trg_mcp_cp7_candidate_guard_monotonic
        BEFORE UPDATE ON mcp_cp7_candidate_guard
        FOR EACH ROW EXECUTE FUNCTION maf_enforce_cp7_candidate_guard_monotonic();
    END IF;
END
$$;""",
            """DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_mcp_cp7_candidate_guard_reject_delete') THEN
        CREATE TRIGGER trg_mcp_cp7_candidate_guard_reject_delete
        BEFORE DELETE ON mcp_cp7_candidate_guard
        FOR EACH ROW EXECUTE FUNCTION maf_reject_append_only_mutation();
    END IF;
END
$$;""",
            """DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_user_mcp_owner_guard_monotonic') THEN
        CREATE TRIGGER trg_user_mcp_owner_guard_monotonic
        BEFORE UPDATE ON user_mcp_owner_mutation_guard
        FOR EACH ROW EXECUTE FUNCTION maf_enforce_user_mcp_owner_guard_monotonic();
    END IF;
END
$$;""",
            """DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_user_mcp_owner_guard_reject_delete') THEN
        CREATE TRIGGER trg_user_mcp_owner_guard_reject_delete
        BEFORE DELETE ON user_mcp_owner_mutation_guard
        FOR EACH ROW EXECUTE FUNCTION maf_reject_append_only_mutation();
    END IF;
END
$$;""",
            """DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_mcp_execution_terminal_projection_monotonic') THEN
        CREATE TRIGGER trg_mcp_execution_terminal_projection_monotonic
        BEFORE UPDATE ON mcp_execution_terminal_projection
        FOR EACH ROW EXECUTE FUNCTION maf_enforce_mcp_terminal_projection_monotonic();
    END IF;
END
$$;""",
            """DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_mcp_execution_terminal_projection_reject_delete') THEN
        CREATE TRIGGER trg_mcp_execution_terminal_projection_reject_delete
        BEFORE DELETE ON mcp_execution_terminal_projection
        FOR EACH ROW EXECUTE FUNCTION maf_reject_append_only_mutation();
    END IF;
END
$$;""",
            """DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_mcp_cp7_safety_attestation_window') THEN
        CREATE TRIGGER trg_mcp_cp7_safety_attestation_window
        BEFORE INSERT ON mcp_cp7_safety_ledger
        FOR EACH ROW EXECUTE FUNCTION maf_validate_cp7_safety_attestation_window();
    END IF;
END
$$;""",
        )
    )
    return "\n".join(statements)


def build_full_fresh_cutover_schema_ddl() -> str:
    return "\n".join((build_runtime_schema_ddl(), build_schema_ddl(guarded=True)))


def _manifest_payload(
    *,
    schema_version: str,
    runtime_table_names: tuple[str, ...],
    operational_table_names: tuple[str, ...],
    table_columns: Mapping[str, Mapping[str, str]],
    check_constraints: Mapping[str, Mapping[str, str]],
    append_only_tables: tuple[str, ...],
    trigger_names: tuple[str, ...],
) -> dict[str, object]:
    return {
        "schema_version": schema_version,
        "runtime_table_names": tuple(sorted(runtime_table_names)),
        "operational_table_names": tuple(sorted(operational_table_names)),
        "table_columns": {name: dict(sorted(columns.items())) for name, columns in sorted(table_columns.items())},
        "check_constraints": {
            name: dict(sorted(constraints.items()))
            for name, constraints in sorted(check_constraints.items())
        },
        "append_only_tables": tuple(sorted(append_only_tables)),
        "trigger_names": tuple(sorted(trigger_names)),
    }


def _checksum(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _postgres_type_name(type_: object) -> str:
    dialect = postgresql.dialect()
    return str(type_.compile(dialect=dialect)).lower()


def _state_column_type(column_name: str) -> str:
    if column_name in {"payload", "result", "metadata"}:
        return "jsonb"
    if column_name.endswith("_at") or column_name in {"available_at", "lease_expires_at", "created_at", "updated_at", "completed_at"}:
        return "timestamp with time zone"
    if column_name in {"partition_sequence", "next_sequence"}:
        return "bigint"
    if column_name in {"priority", "attempt_count", "max_attempts"}:
        return "integer"
    if column_name == "status" and column_name not in {"migration_status"}:
        return "state_command_status"
    return "text"
