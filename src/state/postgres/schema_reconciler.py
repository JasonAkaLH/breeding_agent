from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Mapping

from .runtime_schema import (
    PostgresFreshCutoverSchemaManifest,
    build_runtime_mutation_trigger_schema_ddl,
    build_runtime_schema_ddl,
)
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

_CP7_CHECK_TABLES = frozenset(
    {
        "user_mcp_owner_mutation_guard",
        "mcp_no_server_intent",
        "mcp_dispatch_resume_outbox",
        "mcp_pending_tool_action",
        "mcp_terminal_candidate_lifecycle",
        "mcp_durable_result_lifecycle",
        "mcp_dispatch_aggregate_migration",
        "mcp_terminal_result_receipt",
        "mcp_execution_terminal_projection",
        "mcp_cp7_safety_ledger",
        "mcp_cp7_ready_epoch_event",
        "mcp_cp7_candidate_guard",
    }
)

_MCP_AGGREGATE_CONTROLLED_CUTOVER_TABLES = frozenset(
    {"mcp_call_record", "mcp_dispatch_resume_outbox"}
)
_MCP_CALL_ADDITIVE_RESULT_AUTHORITY_COLUMNS = frozenset(
    {"output_schema", "output_schema_sha256", "terminal_result_source"}
)


class ForbiddenPostgresSchemaActionError(ValueError):
    pass


class PostgresSchemaDriftError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SchemaInspection:
    tables: Mapping[str, Mapping[str, str]]
    enum_types: tuple[str, ...] = ()
    check_constraints: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    triggers: tuple[str, ...] = ()

    @classmethod
    def empty(cls) -> "SchemaInspection":
        return cls(tables={}, enum_types=(), check_constraints={}, triggers=())

    @classmethod
    def from_manifest(cls, manifest: PostgresFreshCutoverSchemaManifest) -> "SchemaInspection":
        return cls(
            tables={
                table: dict(columns)
                for table, columns in manifest.table_columns.items()
            },
            enum_types=("state_command_status",),
            check_constraints={
                table: dict(constraints)
                for table, constraints in manifest.check_constraints.items()
            },
            triggers=manifest.trigger_names,
        )

    def with_table_columns(self, table_name: str, columns: Mapping[str, str]) -> "SchemaInspection":
        tables = {name: dict(cols) for name, cols in self.tables.items()}
        tables[table_name] = dict(columns)
        return SchemaInspection(
            tables=tables,
            enum_types=self.enum_types,
            check_constraints=self.check_constraints,
            triggers=self.triggers,
        )


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
    operator_only_actions: list[str] = []
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
        missing_columns = set(expected_columns) - set(actual_columns)
        additive_result_authority_drift = (
            table_name == "mcp_call_record"
            and bool(missing_columns)
            and missing_columns.issubset(_MCP_CALL_ADDITIVE_RESULT_AUTHORITY_COLUMNS)
            and not (set(actual_columns) - set(expected_columns))
        )
        if table_name in _MCP_AGGREGATE_CONTROLLED_CUTOVER_TABLES and (
            (
                set(expected_columns) != set(actual_columns)
                and not additive_result_authority_drift
            )
            or not _checks_match_exactly(
                manifest.check_constraints.get(table_name, {}),
                inspection.check_constraints.get(table_name, {}),
            )
        ):
            operator_only_actions.append(
                f"mcp_dispatch_aggregate_cutover_required:{table_name}"
            )
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
        if table_name == "mcp_remote_task_binding" and "published_at" in expected_columns:
            actions.append(
                SchemaAction(
                    "backfill_mcp_remote_task_publication",
                    "UPDATE mcp_remote_task_binding "
                    "SET published_at = COALESCE(next_poll_at, terminal_at, updated_at) "
                    "WHERE published_at IS NULL AND "
                    "(next_poll_at IS NOT NULL OR terminal_at IS NOT NULL);",
                    table_name,
                )
            )
        if table_name == "task":
            constraint_name = "ck_task_task_mcp_route_reason_code"
            expected_definition = manifest.check_constraints[table_name][constraint_name]
            actual_name, actual_definition = _find_task_route_constraint(
                inspection.check_constraints.get(table_name, {})
            )
            if actual_definition is None:
                actions.extend(
                    _add_check_constraint_actions(
                        table_name, constraint_name, expected_definition
                    )
                )
            elif _route_reason_values(actual_definition) == _route_reason_values(
                expected_definition
            ):
                pass
            else:
                actions.append(
                    SchemaAction(
                        "replace_task_route_reason_constraint",
                        f"ALTER TABLE task DROP CONSTRAINT IF EXISTS {actual_name};",
                        table_name,
                    )
                )
                actions.extend(
                    _add_check_constraint_actions(
                        table_name, constraint_name, expected_definition
                    )
                )
        elif table_name in _CP7_CHECK_TABLES:
            actions.extend(
                _plan_cp7_check_reconciliation(
                    table_name,
                    manifest.check_constraints.get(table_name, {}),
                    inspection.check_constraints.get(table_name, {}),
                )
            )

    if set(manifest.trigger_names) - set(inspection.triggers):
        for statement in _split_sql(build_runtime_mutation_trigger_schema_ddl()):
            actions.append(
                SchemaAction(
                    "install_cp7_mutation_triggers",
                    statement,
                    None,
                )
            )
    plan = SchemaReconciliationPlan(
        tuple(actions), tuple(sorted(set(operator_only_actions)))
    )
    assert_no_forbidden_schema_sql(plan.sql_script())
    return plan


def _checks_match_exactly(
    expected_checks: Mapping[str, str], actual_checks: Mapping[str, str]
) -> bool:
    return set(expected_checks) == set(actual_checks) and all(
        _normalize_check(actual_checks[name]) == _normalize_check(definition)
        for name, definition in expected_checks.items()
    )


def _add_column_sql(table_name: str, column_name: str, expected_type: str) -> str:
    if table_name == "auth_user_token" and column_name == "auth_generation":
        return "ALTER TABLE auth_user_token ADD COLUMN IF NOT EXISTS auth_generation bigint NOT NULL DEFAULT 0;"
    if table_name == "auth_user_token" and column_name == "auth_generation_updated_at":
        return (
            "ALTER TABLE auth_user_token ADD COLUMN IF NOT EXISTS "
            "auth_generation_updated_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP;"
        )
    return f"ALTER TABLE {table_name} ADD COLUMN {column_name} {expected_type};"


def _add_check_constraint_actions(
    table_name: str,
    constraint_name: str,
    definition: str,
) -> tuple[SchemaAction, SchemaAction]:
    return (
        SchemaAction(
            "add_check_constraint",
            f"ALTER TABLE {table_name} ADD CONSTRAINT {constraint_name} "
            f"CHECK ({definition}) NOT VALID;",
            table_name,
        ),
        SchemaAction(
            "validate_check_constraint",
            f"ALTER TABLE {table_name} VALIDATE CONSTRAINT {constraint_name};",
            table_name,
        ),
    )


def _plan_cp7_check_reconciliation(
    table_name: str,
    expected_checks: Mapping[str, str],
    actual_checks: Mapping[str, str],
) -> tuple[SchemaAction, ...]:
    actions: list[SchemaAction] = []
    for constraint_name, expected_definition in expected_checks.items():
        actual_definition = actual_checks.get(constraint_name)
        if actual_definition is None:
            actions.extend(
                _add_check_constraint_actions(
                    table_name, constraint_name, expected_definition
                )
            )
            continue
        if _normalize_check(actual_definition) == _normalize_check(
            expected_definition
        ):
            continue
        actions.append(
            SchemaAction(
                "replace_cp7_check_constraint",
                f"ALTER TABLE {table_name} DROP CONSTRAINT IF EXISTS "
                f"{constraint_name};",
                table_name,
            )
        )
        actions.extend(
            _add_check_constraint_actions(
                table_name, constraint_name, expected_definition
            )
        )
    for constraint_name in sorted(set(actual_checks) - set(expected_checks)):
        actions.append(
            SchemaAction(
                "drop_unknown_cp7_check_constraint",
                f"ALTER TABLE {table_name} DROP CONSTRAINT IF EXISTS "
                f"{constraint_name};",
                table_name,
            )
        )
    return tuple(actions)


def _find_task_route_constraint(
    constraints: Mapping[str, str],
) -> tuple[str | None, str | None]:
    for name in (
        "ck_task_task_mcp_route_reason_code",
        "task_mcp_route_reason_code",
    ):
        if name in constraints:
            return name, constraints[name]
    for name, definition in constraints.items():
        if "mcp_route_reason_code" in definition:
            return name, definition
    return None, None


def _route_reason_values(definition: str) -> set[str]:
    return set(re.findall(r"'([^']+)'(?:::[a-z ]+)?", definition))


def _normalize_check(definition: str) -> str:
    normalized = definition.lower().strip()
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
    normalized = re.sub(r'[()\s"]+', "", normalized)
    return normalized


def _is_safe_route_reason_extension(actual: str, expected: str) -> bool:
    if "mcp_route_reason_code" not in actual or "mcp_route_reason_code" not in expected:
        return False
    actual_values = _route_reason_values(actual)
    expected_values = _route_reason_values(expected)
    return bool(actual_values) and actual_values < expected_values


def _split_sql(script: str) -> list[str]:
    statements: list[str] = []
    current: list[str] = []
    in_dollar_block = False
    for line in script.splitlines():
        current.append(line)
        stripped = line.strip()
        if stripped.upper().startswith("DO $$") or " AS $$" in stripped.upper():
            in_dollar_block = True
        if in_dollar_block and stripped == "$$;":
            statements.append("\n".join(current).strip())
            current = []
            in_dollar_block = False
        elif not in_dollar_block and stripped.endswith(";"):
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
