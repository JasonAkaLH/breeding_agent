from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping

from .runtime_schema import PostgresFreshCutoverSchemaManifest, build_runtime_schema_ddl
from .schema import build_schema_ddl

_FORBIDDEN_PATTERNS = (
    re.compile(r"\bdrop\s+table\b", re.IGNORECASE),
    re.compile(r"\bdrop\s+database\b", re.IGNORECASE),
    re.compile(r"\btruncate\b", re.IGNORECASE),
    re.compile(r"\balter\s+table\b[^;]*\bdrop\s+column\b", re.IGNORECASE | re.DOTALL),
    re.compile(r"\balter\s+table\b[^;]*\brename\s+to\b", re.IGNORECASE | re.DOTALL),
    re.compile(r"\bdelete\s+from\b", re.IGNORECASE),
    re.compile(r"\bdrop\s+index\b", re.IGNORECASE),
)


class ForbiddenPostgresSchemaActionError(ValueError):
    pass


class PostgresSchemaDriftError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SchemaInspection:
    tables: Mapping[str, Mapping[str, str]]
    enum_types: tuple[str, ...] = ()

    @classmethod
    def empty(cls) -> "SchemaInspection":
        return cls(tables={}, enum_types=())

    @classmethod
    def from_manifest(cls, manifest: PostgresFreshCutoverSchemaManifest) -> "SchemaInspection":
        return cls(tables={table: dict(columns) for table, columns in manifest.table_columns.items()}, enum_types=("state_command_status",))

    def with_table_columns(self, table_name: str, columns: Mapping[str, str]) -> "SchemaInspection":
        tables = {name: dict(cols) for name, cols in self.tables.items()}
        tables[table_name] = dict(columns)
        return SchemaInspection(tables=tables, enum_types=self.enum_types)


@dataclass(frozen=True, slots=True)
class SchemaAction:
    kind: str
    sql: str
    table_name: str | None = None


@dataclass(frozen=True, slots=True)
class SchemaReconciliationPlan:
    actions: tuple[SchemaAction, ...]
    operator_only_actions: tuple[str, ...] = ()

    def sql_script(self) -> str:
        return "\n".join(action.sql for action in self.actions)


def assert_no_forbidden_schema_sql(sql: str) -> None:
    for pattern in _FORBIDDEN_PATTERNS:
        if pattern.search(sql):
            raise ForbiddenPostgresSchemaActionError(f"Forbidden PostgreSQL schema SQL matched: {pattern.pattern}")


def plan_postgres_schema_reconciliation(
    manifest: PostgresFreshCutoverSchemaManifest,
    inspection: SchemaInspection,
) -> SchemaReconciliationPlan:
    actions: list[SchemaAction] = []
    missing_tables = [name for name in manifest.runtime_table_names if name not in inspection.tables]
    missing_state_tables = [name for name in manifest.operational_table_names if name not in inspection.tables]
    if missing_tables:
        for statement in _split_sql(build_runtime_schema_ddl()):
            if statement.strip():
                actions.append(SchemaAction("create_runtime_schema", statement, None))
    if missing_state_tables or "state_command_status" not in inspection.enum_types:
        for statement in _split_sql(build_schema_ddl(guarded=True)):
            if statement.strip():
                actions.append(SchemaAction("create_state_schema", statement, None))

    for table_name in manifest.runtime_table_names:
        expected_columns = manifest.table_columns[table_name]
        actual_columns = inspection.tables.get(table_name)
        if actual_columns is None:
            continue
        for column_name, expected_type in expected_columns.items():
            actual_type = actual_columns.get(column_name)
            if actual_type is None:
                sql = _add_column_sql(table_name, column_name, expected_type)
                actions.append(SchemaAction("add_column", sql, table_name))
                continue
            if _normalize_type(actual_type) != _normalize_type(expected_type):
                raise PostgresSchemaDriftError(
                    f"PostgreSQL schema drift for {table_name}.{column_name}: expected {expected_type}, actual {actual_type}; safe action unavailable"
                )
    plan = SchemaReconciliationPlan(tuple(actions))
    assert_no_forbidden_schema_sql(plan.sql_script())
    return plan


def _add_column_sql(table_name: str, column_name: str, expected_type: str) -> str:
    if table_name == "auth_user_token" and column_name == "auth_generation":
        return "ALTER TABLE auth_user_token ADD COLUMN IF NOT EXISTS auth_generation bigint NOT NULL DEFAULT 0;"
    if table_name == "auth_user_token" and column_name == "auth_generation_updated_at":
        return (
            "ALTER TABLE auth_user_token ADD COLUMN IF NOT EXISTS "
            "auth_generation_updated_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP;"
        )
    return f"ALTER TABLE {table_name} ADD COLUMN {column_name} {expected_type};"


def _split_sql(script: str) -> list[str]:
    statements: list[str] = []
    current: list[str] = []
    in_do_block = False
    for line in script.splitlines():
        current.append(line)
        stripped = line.strip()
        if stripped.upper().startswith("DO $$"):
            in_do_block = True
        if in_do_block and stripped == "$$;":
            statements.append("\n".join(current).strip())
            current = []
            in_do_block = False
        elif not in_do_block and stripped.endswith(";"):
            statements.append("\n".join(current).strip())
            current = []
    if current:
        statements.append("\n".join(current).strip())
    return statements


def _normalize_type(value: str) -> str:
    normalized = " ".join(str(value).lower().split())
    aliases = {
        "timestamp with time zone": "timestamptz",
        "timestamp without time zone": "timestamp",
        "character varying": "text",
    }
    return aliases.get(normalized, normalized)
