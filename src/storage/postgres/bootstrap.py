from __future__ import annotations

from sqlalchemy import Engine, inspect, text

from src.state.postgres.schema import build_schema_ddl
from src.state.postgres.runtime_schema import (
    POSTGRES_CP7_TRIGGER_NAMES,
    build_postgres_fresh_cutover_schema_manifest,
    build_runtime_index_schema_ddl,
    build_runtime_mutation_trigger_schema_ddl,
    build_runtime_table_schema_ddl,
)
from src.state.postgres.schema_reconciler import (
    PostgresSchemaDriftError,
    SchemaInspection,
    assert_no_forbidden_schema_sql,
    plan_postgres_schema_reconciliation,
)


def bootstrap_postgres_database(engine: Engine) -> None:
    """Create the fresh PostgreSQL canonical schema with no destructive actions."""
    table_sql_script = "\n".join((build_runtime_table_schema_ddl(), build_schema_ddl(guarded=True)))
    index_sql_script = build_runtime_index_schema_ddl()
    trigger_sql_script = build_runtime_mutation_trigger_schema_ddl()
    assert_no_forbidden_schema_sql(
        "\n".join((table_sql_script, index_sql_script, trigger_sql_script))
    )
    with engine.begin() as connection:
        connection.execute(text("SET LOCAL lock_timeout = '3s'"))
        connection.execute(text("SET LOCAL statement_timeout = '30s'"))
        connection.execute(text("SELECT pg_advisory_xact_lock(hashtext('breeding_agent_schema_bootstrap'))"))
        initial_plan = plan_postgres_schema_reconciliation(
            build_postgres_fresh_cutover_schema_manifest(),
            _inspect_current_schema(connection),
        )
        if initial_plan.operator_only_actions:
            raise PostgresSchemaDriftError(
                "mcp_dispatch_aggregate_migration_required"
            )
        for statement in _split_sql(table_sql_script):
            if statement.strip():
                connection.execute(text(statement))
        plan = plan_postgres_schema_reconciliation(
            build_postgres_fresh_cutover_schema_manifest(),
            _inspect_current_schema(connection),
        )
        for action in plan.actions:
            connection.execute(text(action.sql))
        for statement in _split_sql(index_sql_script):
            if statement.strip():
                connection.execute(text(statement))


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


def _inspect_current_schema(connection) -> SchemaInspection:
    inspector = inspect(connection)
    tables: dict[str, dict[str, str]] = {}
    check_constraints: dict[str, dict[str, str]] = {}
    for table_name in inspector.get_table_names():
        tables[table_name] = {column["name"]: _column_type_name(column) for column in inspector.get_columns(table_name)}
        check_constraints[table_name] = {
            str(constraint["name"]): str(constraint["sqltext"])
            for constraint in inspector.get_check_constraints(table_name)
            if constraint.get("name") and constraint.get("sqltext")
        }
    enum_types = tuple(
        row[0]
        for row in connection.execute(
            text("SELECT typname FROM pg_type WHERE typname = 'state_command_status'")
        ).all()
    )
    known_trigger_names = set(POSTGRES_CP7_TRIGGER_NAMES)
    triggers = tuple(
        row[0]
        for row in connection.execute(
            text("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal")
        ).all()
        if row[0] in known_trigger_names
    )
    return SchemaInspection(
        tables=tables,
        enum_types=enum_types,
        check_constraints=check_constraints,
        triggers=triggers,
    )


def _column_type_name(column: dict[str, object]) -> str:
    column_type = column["type"]
    rendered = str(column_type).lower()
    if rendered == "timestamp" and bool(getattr(column_type, "timezone", False)):
        return "timestamp with time zone"
    return rendered
