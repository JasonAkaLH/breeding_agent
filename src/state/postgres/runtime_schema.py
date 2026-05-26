from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Mapping

from sqlalchemy.schema import CreateIndex, CreateTable
from sqlalchemy.dialects import postgresql

# Importing models registers all runtime tables on SQLiteBase.metadata.
import src.storage.sqlite.models  # noqa: F401
from src.storage.sqlite.base import SQLiteBase

from .schema import POSTGRES_STATE_TABLES, build_schema_ddl

POSTGRES_RUNTIME_SCHEMA_VERSION = "maf.postgresql_fresh_runtime_schema.v1"
POSTGRES_RUNTIME_TABLES = tuple(sorted(SQLiteBase.metadata.tables))


@dataclass(frozen=True, slots=True)
class PostgresFreshCutoverSchemaManifest:
    schema_version: str
    runtime_table_names: tuple[str, ...]
    operational_table_names: tuple[str, ...]
    table_columns: Mapping[str, Mapping[str, str]]
    checksum: str

    def with_runtime_table_names(self, names: tuple[str, ...]) -> "PostgresFreshCutoverSchemaManifest":
        columns = dict(self.table_columns)
        payload = _manifest_payload(
            schema_version=self.schema_version,
            runtime_table_names=names,
            operational_table_names=self.operational_table_names,
            table_columns=columns,
        )
        return PostgresFreshCutoverSchemaManifest(
            schema_version=self.schema_version,
            runtime_table_names=names,
            operational_table_names=self.operational_table_names,
            table_columns=columns,
            checksum=_checksum(payload),
        )


def build_postgres_fresh_cutover_schema_manifest() -> PostgresFreshCutoverSchemaManifest:
    table_columns: dict[str, dict[str, str]] = {}
    for table_name, table in sorted(SQLiteBase.metadata.tables.items()):
        table_columns[table_name] = {column.name: _postgres_type_name(column.type) for column in table.columns}
    operational = tuple(sorted(POSTGRES_STATE_TABLES))
    for table_name in operational:
        descriptor = POSTGRES_STATE_TABLES[table_name]
        table_columns[table_name] = {column: _state_column_type(column) for column in descriptor.columns}
    payload = _manifest_payload(
        schema_version=POSTGRES_RUNTIME_SCHEMA_VERSION,
        runtime_table_names=POSTGRES_RUNTIME_TABLES,
        operational_table_names=operational,
        table_columns=table_columns,
    )
    return PostgresFreshCutoverSchemaManifest(
        schema_version=POSTGRES_RUNTIME_SCHEMA_VERSION,
        runtime_table_names=POSTGRES_RUNTIME_TABLES,
        operational_table_names=operational,
        table_columns=table_columns,
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
    return "\n".join((build_runtime_table_schema_ddl(), build_runtime_index_schema_ddl()))


def build_full_fresh_cutover_schema_ddl() -> str:
    return "\n".join((build_runtime_schema_ddl(), build_schema_ddl(guarded=True)))


def _manifest_payload(
    *,
    schema_version: str,
    runtime_table_names: tuple[str, ...],
    operational_table_names: tuple[str, ...],
    table_columns: Mapping[str, Mapping[str, str]],
) -> dict[str, object]:
    return {
        "schema_version": schema_version,
        "runtime_table_names": tuple(sorted(runtime_table_names)),
        "operational_table_names": tuple(sorted(operational_table_names)),
        "table_columns": {name: dict(sorted(columns.items())) for name, columns in sorted(table_columns.items())},
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
