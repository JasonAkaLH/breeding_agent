from __future__ import annotations

from sqlalchemy import Connection, Engine, inspect, text

from .base import SQLiteBase


LEGACY_AUTH_TABLES = (
    "auth_user",
    "auth_captcha_challenge",
    "auth_session",
    "auth_api_token",
)


def bootstrap_sqlite_database(engine: Engine) -> None:
    _migrate_username_owner_columns(engine)
    _migrate_message_public_columns(engine)
    _drop_legacy_auth_tables(engine)
    SQLiteBase.metadata.create_all(engine)


def _drop_legacy_auth_tables(engine: Engine) -> None:
    with engine.begin() as connection:
        for table_name in LEGACY_AUTH_TABLES:
            connection.execute(text(f"DROP TABLE IF EXISTS {_quote(connection, table_name)}"))


def _migrate_username_owner_columns(engine: Engine) -> None:
    """Convert legacy account_id owner columns into username columns.

    SQLite `create_all` will not add missing columns to existing tables, so the
    hard switch to username-owned models needs a small pre-create migration for
    already-deployed local databases. Leaving the old `account_id NOT NULL`
    column in place breaks future inserts because new code only writes
    `username`, so legacy owner tables are rebuilt against the current SQLAlchemy
    metadata after the values are backfilled.
    """
    table_columns = {
        "conversation": ("username", "account_id"),
        "conversation_memory_summary": ("username", "account_id"),
        "conversation_pending_skill_context": ("username", "account_id"),
    }
    with engine.begin() as connection:
        existing_tables = set(inspect(connection).get_table_names())
        for table_name, (new_column, old_column) in table_columns.items():
            if table_name not in existing_tables:
                continue
            columns = {column["name"] for column in inspect(connection).get_columns(table_name)}
            if old_column not in columns:
                continue
            if new_column not in columns:
                connection.execute(
                    text(
                        f"ALTER TABLE {_quote(connection, table_name)} "
                        f"ADD COLUMN {_quote(connection, new_column)} TEXT"
                    )
                )
            connection.execute(
                text(
                    f"UPDATE {_quote(connection, table_name)} "
                    f"SET {_quote(connection, new_column)} = {_quote(connection, old_column)} "
                    f"WHERE {_quote(connection, new_column)} IS NULL"
                )
            )
            _rebuild_table_without_legacy_owner(connection, table_name, old_column)


def _migrate_message_public_columns(engine: Engine) -> None:
    """Add public-history message columns to existing SQLite message tables.

    SQLAlchemy `create_all` does not alter existing tables. The file upload
    history projection reuses the message table, so legacy local databases need
    the new nullable/defaulted columns before metadata is created.
    """
    with engine.begin() as connection:
        existing_tables = set(inspect(connection).get_table_names())
        if "message" not in existing_tables:
            return
        columns = {column["name"] for column in inspect(connection).get_columns("message")}
        quoted_table = _quote(connection, "message")
        if "message_type" not in columns:
            connection.execute(
                text(f"ALTER TABLE {quoted_table} ADD COLUMN {_quote(connection, 'message_type')} TEXT NOT NULL DEFAULT 'chat'")
            )
        else:
            connection.execute(
                text(
                    f"UPDATE {quoted_table} SET {_quote(connection, 'message_type')} = 'chat' "
                    f"WHERE {_quote(connection, 'message_type')} IS NULL OR {_quote(connection, 'message_type')} = ''"
                )
            )
        if "metadata" not in columns:
            connection.execute(
                text(f"ALTER TABLE {quoted_table} ADD COLUMN {_quote(connection, 'metadata')} TEXT NOT NULL DEFAULT '{{}}'")
            )
        else:
            connection.execute(
                text(
                    f"UPDATE {quoted_table} SET {_quote(connection, 'metadata')} = '{{}}' "
                    f"WHERE {_quote(connection, 'metadata')} IS NULL OR trim({_quote(connection, 'metadata')}) = ''"
                )
            )
        if "updated_at" not in columns:
            connection.execute(
                text(f"ALTER TABLE {quoted_table} ADD COLUMN {_quote(connection, 'updated_at')} TEXT")
            )


def _rebuild_table_without_legacy_owner(connection: Connection, table_name: str, old_column: str) -> None:
    target_table = SQLiteBase.metadata.tables[table_name]
    _validate_rebuild_source_columns(connection, table_name, old_column)
    temp_table_name = f"__maf_legacy_{table_name}"
    quoted_table = _quote(connection, table_name)
    quoted_temp_table = _quote(connection, temp_table_name)

    old_row_count = _table_row_count(connection, table_name)
    connection.execute(text(f"DROP TABLE IF EXISTS {quoted_temp_table}"))
    connection.execute(text(f"ALTER TABLE {quoted_table} RENAME TO {quoted_temp_table}"))
    _drop_indexes_for_table(connection, temp_table_name)

    target_table.create(bind=connection, checkfirst=False)

    temp_columns = {column["name"] for column in inspect(connection).get_columns(temp_table_name)}
    target_columns = [column.name for column in target_table.columns]
    insert_columns = ", ".join(_quote(connection, column) for column in target_columns)
    select_expressions = ", ".join(
        _legacy_select_expression(connection, column, temp_columns, old_column) for column in target_columns
    )
    connection.execute(
        text(
            f"INSERT INTO {quoted_table} ({insert_columns}) "
            f"SELECT {select_expressions} FROM {quoted_temp_table}"
        )
    )
    new_row_count = _table_row_count(connection, table_name)
    if new_row_count != old_row_count:
        raise RuntimeError(
            f"SQLite username migration copied {new_row_count} rows from {table_name}; expected {old_row_count}"
        )
    connection.execute(text(f"DROP TABLE {quoted_temp_table}"))


def _validate_rebuild_source_columns(connection: Connection, table_name: str, old_column: str) -> None:
    existing_columns = {column["name"] for column in inspect(connection).get_columns(table_name)}
    target_table = SQLiteBase.metadata.tables[table_name]
    missing_required_columns = [
        column.name
        for column in target_table.columns
        if column.name not in existing_columns
        and not (column.name == "username" and old_column in existing_columns)
        and not column.nullable
        and column.default is None
        and column.server_default is None
    ]
    if missing_required_columns:
        raise RuntimeError(
            f"SQLite username migration cannot rebuild {table_name}; "
            f"missing required columns: {', '.join(missing_required_columns)}"
        )


def _table_row_count(connection: Connection, table_name: str) -> int:
    return int(connection.execute(text(f"SELECT COUNT(*) FROM {_quote(connection, table_name)}")).scalar_one())


def _drop_indexes_for_table(connection: Connection, table_name: str) -> None:
    for index in inspect(connection).get_indexes(table_name):
        index_name = index.get("name")
        if index_name:
            connection.execute(text(f"DROP INDEX IF EXISTS {_quote(connection, index_name)}"))


def _legacy_select_expression(
    connection: Connection,
    column_name: str,
    temp_columns: set[str],
    old_column: str,
) -> str:
    quoted_column = _quote(connection, column_name)
    if column_name in temp_columns:
        return quoted_column
    if column_name == "username" and old_column in temp_columns:
        return _quote(connection, old_column)
    return "NULL"


def _quote(connection: Connection, identifier: str) -> str:
    return connection.dialect.identifier_preparer.quote(identifier)
