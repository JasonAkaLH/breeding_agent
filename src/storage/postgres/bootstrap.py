from __future__ import annotations

from sqlalchemy import Engine, inspect, text

from src.state.postgres.schema import build_schema_ddl
from src.state.postgres.runtime_schema import build_postgres_fresh_cutover_schema_manifest, build_runtime_index_schema_ddl, build_runtime_table_schema_ddl
from src.state.postgres.schema_reconciler import SchemaInspection, assert_no_forbidden_schema_sql, plan_postgres_schema_reconciliation


def bootstrap_postgres_database(engine: Engine) -> None:
    """Create the fresh PostgreSQL canonical schema with no destructive actions."""
    table_sql_script = "\n".join((build_runtime_table_schema_ddl(), build_schema_ddl(guarded=True)))
    index_sql_script = build_runtime_index_schema_ddl()
    assert_no_forbidden_schema_sql("\n".join((table_sql_script, index_sql_script)))
    with engine.begin() as connection:
        connection.execute(text("SET LOCAL lock_timeout = '3s'"))
        connection.execute(text("SET LOCAL statement_timeout = '30s'"))
        connection.execute(text("SELECT pg_advisory_xact_lock(hashtext('breeding_agent_schema_bootstrap'))"))
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


def _inspect_current_schema(connection) -> SchemaInspection:
    inspector = inspect(connection)
    tables: dict[str, dict[str, str]] = {}
    for table_name in inspector.get_table_names():
        tables[table_name] = {column["name"]: _column_type_name(column) for column in inspector.get_columns(table_name)}
    enum_types = tuple(
        row[0]
        for row in connection.execute(
            text("SELECT typname FROM pg_type WHERE typname = 'state_command_status'")
        ).all()
    )
    return SchemaInspection(tables=tables, enum_types=enum_types)


def _column_type_name(column: dict[str, object]) -> str:
    column_type = column["type"]
    rendered = str(column_type).lower()
    if rendered == "timestamp" and bool(getattr(column_type, "timezone", False)):
        return "timestamp with time zone"
    return rendered
