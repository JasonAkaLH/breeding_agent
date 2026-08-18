from __future__ import annotations

import contextlib
import hashlib
import os
import sqlite3
import stat
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from sqlalchemy import Engine, inspect, text

from src.integrations.mcp.cp7_artifacts import canonical_sha256
from src.state.postgres.runtime_schema import POSTGRES_RUNTIME_SCHEMA_VERSION
from src.state.postgres.runtime_schema import (
    build_postgres_fresh_cutover_schema_manifest,
)
from src.state.postgres.schema_reconciler import SchemaInspection
from src.storage.sqlite.base import SQLiteBase


MCP_DISPATCH_AGGREGATE_REPORT_SCHEMA = (
    "maf.user_mcp.dispatch_aggregate_migration_report.v1"
)
MCP_DISPATCH_AGGREGATE_SCHEMA_VERSION = POSTGRES_RUNTIME_SCHEMA_VERSION

_OUTBOX_STATUSES = (
    "pending",
    "claimed",
    "active",
    "waiting_approval",
    "waiting_input",
    "remote_pending",
    "completed",
    "aborted",
)
_CALL_STATUSES = (
    "reserved",
    "active",
    "completed",
    "failed",
    "cancelled",
    "input_required",
    "remote_pending",
    "unknown",
)
_MIGRATION_TRANSITIONS = {
    "planned": frozenset({"backed_up", "applying", "failed"}),
    "backed_up": frozenset({"applying", "failed"}),
    "applying": frozenset({"applied", "failed"}),
    "applied": frozenset(),
    "failed": frozenset(),
}


class MCPDispatchAggregateMigrationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class MCPDispatchAggregateMigrationReport:
    backend: str
    table_states: Mapping[str, str]
    row_counts: Mapping[str, int]
    status_counts: Mapping[str, Mapping[str, int]]
    blocker_reason_codes: tuple[str, ...]

    @property
    def migration_required(self) -> bool:
        return any(state == "legacy" for state in self.table_states.values())

    @property
    def apply_eligible(self) -> bool:
        return self.migration_required and not self.blocker_reason_codes

    def payload_without_sha(self) -> dict[str, object]:
        return {
            "schema": MCP_DISPATCH_AGGREGATE_REPORT_SCHEMA,
            "backend": self.backend,
            "schema_version": MCP_DISPATCH_AGGREGATE_SCHEMA_VERSION,
            "table_states": dict(sorted(self.table_states.items())),
            "row_counts": dict(sorted(self.row_counts.items())),
            "status_counts": {
                table: dict(sorted(counts.items()))
                for table, counts in sorted(self.status_counts.items())
            },
            "migration_required": self.migration_required,
            "apply_eligible": self.apply_eligible,
            "blocker_reason_codes": list(self.blocker_reason_codes),
        }

    def as_payload(self) -> dict[str, object]:
        payload = self.payload_without_sha()
        payload["report_sha256"] = canonical_sha256(payload)
        return payload


@dataclass(frozen=True, slots=True)
class SQLiteAggregateBackup:
    basename: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class PostgresAggregateCutoverPlan:
    statements: tuple[str, ...]

    @property
    def plan_sha256(self) -> str:
        return canonical_sha256({"statements": list(self.statements)})


def validate_migration_transition(current: str, target: str) -> None:
    allowed = _MIGRATION_TRANSITIONS.get(str(current))
    if allowed is None or str(target) not in allowed:
        raise MCPDispatchAggregateMigrationError(
            "mcp_dispatch_aggregate_migration_transition_invalid"
        )


def build_postgres_aggregate_cutover_plan(
    inspection: SchemaInspection,
) -> PostgresAggregateCutoverPlan:
    manifest = build_postgres_fresh_cutover_schema_manifest()
    statements = [
        "SET LOCAL lock_timeout = '3s';",
        "SET LOCAL statement_timeout = '30s';",
        "SELECT pg_advisory_xact_lock(hashtext('mcp_dispatch_aggregate_v1'));",
    ]
    for table_name in ("mcp_call_record", "mcp_dispatch_resume_outbox"):
        actual_columns = inspection.tables.get(table_name)
        if actual_columns is None:
            raise MCPDispatchAggregateMigrationError(
                "mcp_dispatch_aggregate_postgres_table_missing"
            )
        expected_columns = manifest.table_columns[table_name]
        for column_name in sorted(set(expected_columns) - set(actual_columns)):
            statements.append(
                _postgres_add_aggregate_column(
                    table_name,
                    column_name,
                    expected_columns[column_name],
                )
            )
        expected_checks = manifest.check_constraints.get(table_name, {})
        actual_checks = inspection.check_constraints.get(table_name, {})
        for constraint_name, definition in sorted(expected_checks.items()):
            if (
                constraint_name in actual_checks
                and _normalize_contract_sql(actual_checks[constraint_name])
                == _normalize_contract_sql(definition)
            ):
                continue
            temporary_name = (
                "mcpagg_v6_"
                + hashlib.sha256(
                    f"{table_name}:{constraint_name}".encode("utf-8")
                ).hexdigest()[:16]
            )
            statements.extend(
                (
                    f"ALTER TABLE {table_name} ADD CONSTRAINT {temporary_name} "
                    f"CHECK ({definition}) NOT VALID;",
                    f"ALTER TABLE {table_name} VALIDATE CONSTRAINT {temporary_name};",
                    f"ALTER TABLE {table_name} DROP CONSTRAINT IF EXISTS "
                    f"{constraint_name};",
                    f"ALTER TABLE {table_name} RENAME CONSTRAINT {temporary_name} "
                    f"TO {constraint_name};",
                )
            )
        for constraint_name in sorted(set(actual_checks) - set(expected_checks)):
            if re.fullmatch(r"[a-z_][a-z0-9_]*", constraint_name) is None:
                raise MCPDispatchAggregateMigrationError(
                    "mcp_dispatch_aggregate_postgres_constraint_name_invalid"
                )
            statements.append(
                f"ALTER TABLE {table_name} DROP CONSTRAINT IF EXISTS "
                f"{constraint_name};"
            )
    statements.extend(
        (
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_mcp_call_pending_action "
            "ON mcp_call_record (pending_action_id) "
            "WHERE pending_action_id IS NOT NULL;",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_mcp_call_continuation_source "
            "ON mcp_call_record (continuation_of_call_ref) "
            "WHERE continuation_of_call_ref IS NOT NULL;",
            "CREATE INDEX IF NOT EXISTS idx_mcp_dispatch_resume_status_keyset "
            "ON mcp_dispatch_resume_outbox (status, updated_at, outbox_id);",
        )
    )
    return PostgresAggregateCutoverPlan(tuple(statements))


def inspect_sqlite_dispatch_aggregate(
    database_path: str | os.PathLike[str],
) -> MCPDispatchAggregateMigrationReport:
    path = Path(database_path)
    if str(database_path) == ":memory:":
        raise MCPDispatchAggregateMigrationError(
            "mcp_dispatch_aggregate_report_requires_file_database"
        )
    connection = sqlite3.connect(
        f"file:{path.resolve(strict=True)}?mode=ro", uri=True
    )
    try:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        table_states: dict[str, str] = {}
        row_counts: dict[str, int] = {}
        status_counts: dict[str, dict[str, int]] = {}
        blockers: list[str] = []
        for table_name, statuses in (
            ("mcp_dispatch_resume_outbox", _OUTBOX_STATUSES),
            ("mcp_call_record", _CALL_STATUSES),
        ):
            if table_name not in tables:
                table_states[table_name] = "absent"
                row_counts[table_name] = 0
                status_counts[table_name] = _empty_status_counts(statuses)
                continue
            columns = {
                str(row[1])
                for row in connection.execute(
                    f'PRAGMA table_info("{table_name}")'
                )
            }
            expected_columns = set(
                SQLiteBase.metadata.tables[table_name].columns.keys()
            )
            state = (
                "final"
                if columns == expected_columns
                and _sqlite_table_contract_matches(
                    connection, table_name
                )
                else "legacy"
            )
            table_states[table_name] = state
            count = int(
                connection.execute(
                    f'SELECT COUNT(*) FROM "{table_name}"'
                ).fetchone()[0]
            )
            row_counts[table_name] = count
            counts = _sqlite_status_counts(connection, table_name, statuses)
            status_counts[table_name] = counts
            if counts["other"]:
                blockers.append(f"{table_name}_unknown_status_rows")
            if state == "legacy" and count:
                blockers.append(f"{table_name}_business_rows_require_writer")
        return MCPDispatchAggregateMigrationReport(
            backend="sqlite",
            table_states=table_states,
            row_counts=row_counts,
            status_counts=status_counts,
            blocker_reason_codes=tuple(sorted(set(blockers))),
        )
    finally:
        connection.close()


def inspect_postgres_dispatch_aggregate(
    engine: Engine,
) -> MCPDispatchAggregateMigrationReport:
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    table_states: dict[str, str] = {}
    row_counts: dict[str, int] = {}
    status_counts: dict[str, dict[str, int]] = {}
    blockers: list[str] = []
    with engine.connect() as connection:
        for table_name, statuses in (
            ("mcp_dispatch_resume_outbox", _OUTBOX_STATUSES),
            ("mcp_call_record", _CALL_STATUSES),
        ):
            if table_name not in tables:
                table_states[table_name] = "absent"
                row_counts[table_name] = 0
                status_counts[table_name] = _empty_status_counts(statuses)
                continue
            columns = {
                str(column["name"])
                for column in inspector.get_columns(table_name)
            }
            expected_columns = set(
                SQLiteBase.metadata.tables[table_name].columns.keys()
            )
            state = (
                "final"
                if columns == expected_columns
                and _postgres_table_contract_matches(inspector, table_name)
                else "legacy"
            )
            table_states[table_name] = state
            count = int(
                connection.execute(
                    text(f'SELECT COUNT(*) FROM "{table_name}"')
                ).scalar_one()
            )
            row_counts[table_name] = count
            counts = _sqlalchemy_status_counts(
                connection, table_name, statuses
            )
            status_counts[table_name] = counts
            if counts["other"]:
                blockers.append(f"{table_name}_unknown_status_rows")
            if state == "legacy" and count:
                blockers.append(f"{table_name}_business_rows_require_writer")
    return MCPDispatchAggregateMigrationReport(
        backend="postgresql",
        table_states=table_states,
        row_counts=row_counts,
        status_counts=status_counts,
        blocker_reason_codes=tuple(sorted(set(blockers))),
    )


def create_or_adopt_sqlite_aggregate_backup(
    database_path: str | os.PathLike[str],
    *,
    report_sha256: str,
    migration_status: str,
    expected_backup_sha256: str | None = None,
) -> SQLiteAggregateBackup | None:
    if str(database_path) == ":memory:":
        return None
    source = Path(database_path).resolve(strict=True)
    if (
        not report_sha256.startswith("sha256:")
        or len(report_sha256) != 71
        or any(
            character not in "0123456789abcdef"
            for character in report_sha256.removeprefix("sha256:")
        )
    ):
        raise MCPDispatchAggregateMigrationError(
            "mcp_dispatch_aggregate_report_sha_invalid"
        )
    basename = (
        f"{source.name}.pre-mcp-aggregate-v1."
        f"{report_sha256.removeprefix('sha256:')[:12]}.bak"
    )
    backup = source.with_name(basename)
    if backup.exists() or backup.is_symlink():
        if migration_status not in {"backed_up", "applying"}:
            raise MCPDispatchAggregateMigrationError(
                "mcp_dispatch_aggregate_backup_state_conflict"
            )
        descriptor = _validate_sqlite_backup(backup)
        if expected_backup_sha256 != descriptor.sha256:
            raise MCPDispatchAggregateMigrationError(
                "mcp_dispatch_aggregate_backup_digest_conflict"
            )
        return descriptor
    if migration_status != "planned" or expected_backup_sha256 is not None:
        raise MCPDispatchAggregateMigrationError(
            "mcp_dispatch_aggregate_backup_state_conflict"
        )
    descriptor = os.open(
        backup,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    os.close(descriptor)
    try:
        source_connection = sqlite3.connect(
            f"file:{source}?mode=ro", uri=True
        )
        destination_connection = sqlite3.connect(str(backup))
        try:
            source_connection.backup(destination_connection)
            destination_connection.commit()
        finally:
            destination_connection.close()
            source_connection.close()
        os.chmod(backup, 0o600, follow_symlinks=False)
        _fsync_file(backup)
        _fsync_directory(backup.parent)
        return _validate_sqlite_backup(backup)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            backup.unlink()
        _fsync_directory(backup.parent)
        raise


def _empty_status_counts(statuses: tuple[str, ...]) -> dict[str, int]:
    return {**{status: 0 for status in statuses}, "other": 0}


def _postgres_add_aggregate_column(
    table_name: str, column_name: str, expected_type: str
) -> str:
    definitions = {
        ("mcp_call_record", "pending_action_id"): "text",
        ("mcp_call_record", "continuation_of_call_ref"): "text",
        (
            "mcp_dispatch_resume_outbox",
            "resume_reason",
        ): "text NOT NULL DEFAULT 'initial'",
        ("mcp_dispatch_resume_outbox", "resume_receipt_id"): "text",
        ("mcp_dispatch_resume_outbox", "resume_answer_id"): "text",
        (
            "mcp_dispatch_resume_outbox",
            "selector_step_total",
        ): "bigint NOT NULL DEFAULT 0",
        (
            "mcp_dispatch_resume_outbox",
            "approval_round_total",
        ): "bigint NOT NULL DEFAULT 0",
    }
    definition = definitions.get((table_name, column_name))
    if definition is None or not definition.startswith(expected_type):
        raise MCPDispatchAggregateMigrationError(
            "mcp_dispatch_aggregate_postgres_nonadditive_column_unplanned"
        )
    return (
        f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition};"
    )


def _sqlite_table_contract_matches(
    connection: sqlite3.Connection, table_name: str
) -> bool:
    table = SQLiteBase.metadata.tables[table_name]
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    if row is None or not isinstance(row[0], str):
        return False
    create_sql = _normalize_contract_sql(row[0])
    expected_checks = {
        _normalize_contract_sql(str(constraint.sqltext))
        for constraint in table.constraints
        if constraint.__class__.__name__ == "CheckConstraint"
    }
    if any(check not in create_sql for check in expected_checks):
        return False
    actual_indexes = {
        str(row[1])
        for row in connection.execute(f'PRAGMA index_list("{table_name}")')
        if len(row) >= 4 and str(row[3]) == "c"
    }
    return actual_indexes == {index.name for index in table.indexes}


def _postgres_table_contract_matches(inspector, table_name: str) -> bool:
    table = SQLiteBase.metadata.tables[table_name]
    expected_checks = {
        _normalize_contract_sql(str(constraint.sqltext))
        for constraint in table.constraints
        if constraint.__class__.__name__ == "CheckConstraint"
    }
    actual_checks = {
        _normalize_contract_sql(str(item.get("sqltext") or ""))
        for item in inspector.get_check_constraints(table_name)
    }
    expected_indexes = {index.name for index in table.indexes}
    actual_indexes = {
        str(item.get("name") or "")
        for item in inspector.get_indexes(table_name)
        if item.get("name")
    }
    return expected_checks == actual_checks and expected_indexes == actual_indexes


def _normalize_contract_sql(value: str) -> str:
    normalized = value.lower().strip()
    if normalized.startswith("check"):
        normalized = normalized[5:].strip()
    normalized = re.sub(
        r"::(?:text|character varying|bigint|integer|boolean|timestamp with time zone)",
        "",
        normalized,
    )
    normalized = re.sub(
        r"([a-z_][a-z0-9_]*)\s*=\s*any\s*\(\s*array\[(.*?)\]\s*\)",
        r"\1 in (\2)",
        normalized,
        flags=re.DOTALL,
    )
    return "".join(normalized.replace('"', "").split())


def _sqlite_status_counts(
    connection: sqlite3.Connection,
    table_name: str,
    statuses: tuple[str, ...],
) -> dict[str, int]:
    counts = _empty_status_counts(statuses)
    for status, count in connection.execute(
        f'SELECT status, COUNT(*) FROM "{table_name}" GROUP BY status'
    ):
        key = str(status) if str(status) in counts else "other"
        counts[key] += int(count)
    return counts


def _sqlalchemy_status_counts(
    connection,
    table_name: str,
    statuses: tuple[str, ...],
) -> dict[str, int]:
    counts = _empty_status_counts(statuses)
    rows = connection.execute(
        text(f'SELECT status, COUNT(*) FROM "{table_name}" GROUP BY status')
    ).all()
    for status, count in rows:
        key = str(status) if str(status) in counts else "other"
        counts[key] += int(count)
    return counts


def _validate_sqlite_backup(path: Path) -> SQLiteAggregateBackup:
    metadata = path.stat(follow_symlinks=False)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
    ):
        raise MCPDispatchAggregateMigrationError(
            "mcp_dispatch_aggregate_backup_identity_invalid"
        )
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    digest = hashlib.sha256()
    with os.fdopen(descriptor, "rb") as handle:
        opened = os.fstat(handle.fileno())
        if (
            opened.st_dev != metadata.st_dev
            or opened.st_ino != metadata.st_ino
            or opened.st_nlink != 1
            or stat.S_IMODE(opened.st_mode) != 0o600
        ):
            raise MCPDispatchAggregateMigrationError(
                "mcp_dispatch_aggregate_backup_inode_drift"
            )
        header = handle.read(16)
        if header != b"SQLite format 3\x00":
            raise MCPDispatchAggregateMigrationError(
                "mcp_dispatch_aggregate_backup_header_invalid"
            )
        digest.update(header)
        while chunk := handle.read(64 * 1024):
            digest.update(chunk)
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
    finally:
        connection.close()
    if integrity != ("ok",):
        raise MCPDispatchAggregateMigrationError(
            "mcp_dispatch_aggregate_backup_integrity_invalid"
        )
    return SQLiteAggregateBackup(
        basename=path.name,
        sha256="sha256:" + digest.hexdigest(),
        size_bytes=int(metadata.st_size),
    )


def _fsync_file(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "MCPDispatchAggregateMigrationError",
    "MCPDispatchAggregateMigrationReport",
    "MCP_DISPATCH_AGGREGATE_REPORT_SCHEMA",
    "MCP_DISPATCH_AGGREGATE_SCHEMA_VERSION",
    "SQLiteAggregateBackup",
    "PostgresAggregateCutoverPlan",
    "build_postgres_aggregate_cutover_plan",
    "create_or_adopt_sqlite_aggregate_backup",
    "inspect_postgres_dispatch_aggregate",
    "inspect_sqlite_dispatch_aggregate",
    "validate_migration_transition",
]
