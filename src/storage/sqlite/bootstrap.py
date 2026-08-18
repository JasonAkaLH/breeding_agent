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
    _migrate_user_mcp_grant_invalidation_columns(engine)
    _migrate_mcp_remote_task_claim_columns(engine)
    _migrate_mcp_continuation_command_columns(engine)
    _migrate_mcp_rollout_metric_red_line(engine)
    _migrate_mcp_rollout_evidence_attestation_columns(engine)
    _migrate_mcp_rollout_promotion_block_reasons(engine)
    _migrate_task_mcp_assignment_columns(engine)
    _migrate_task_mcp_route_reasons(engine)
    _migrate_cp7_authority_columns(engine)
    _drop_legacy_auth_tables(engine)
    SQLiteBase.metadata.create_all(engine)
    _ensure_cp7_mutation_triggers(engine)


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


def _migrate_user_mcp_grant_invalidation_columns(engine: Engine) -> None:
    """Add nullable phase-two grant lifecycle fields to existing local databases."""
    with engine.begin() as connection:
        existing_tables = set(inspect(connection).get_table_names())
        if "user_mcp_tool_grant" not in existing_tables:
            return
        columns = {column["name"] for column in inspect(connection).get_columns("user_mcp_tool_grant")}
        quoted_table = _quote(connection, "user_mcp_tool_grant")
        for column_name in ("invalidated_at", "invalid_reason"):
            if column_name not in columns:
                connection.execute(
                    text(
                        f"ALTER TABLE {quoted_table} "
                        f"ADD COLUMN {_quote(connection, column_name)} TEXT"
                    )
                )


def _migrate_mcp_remote_task_claim_columns(engine: Engine) -> None:
    """Add the Phase-3 recovery-worker lease fields to existing local stores."""

    with engine.begin() as connection:
        existing_tables = set(inspect(connection).get_table_names())
        if "mcp_remote_task_binding" not in existing_tables:
            return
        columns = {
            column["name"]
            for column in inspect(connection).get_columns("mcp_remote_task_binding")
        }
        quoted_table = _quote(connection, "mcp_remote_task_binding")
        nullable_text_columns = ("claim_owner", "claim_token", "lease_expires_at")
        for column_name in nullable_text_columns:
            if column_name not in columns:
                connection.execute(
                    text(
                        f"ALTER TABLE {quoted_table} "
                        f"ADD COLUMN {_quote(connection, column_name)} TEXT"
                    )
                )
        if "revision" not in columns:
            connection.execute(
                text(
                    f"ALTER TABLE {quoted_table} "
                    f"ADD COLUMN {_quote(connection, 'revision')} INTEGER NOT NULL DEFAULT 0"
                )
            )
        if "continuation_plan" not in columns:
            connection.execute(
                text(
                    f"ALTER TABLE {quoted_table} "
                    f"ADD COLUMN {_quote(connection, 'continuation_plan')} TEXT"
                )
            )
        if "published_at" not in columns:
            connection.execute(
                text(
                    f"ALTER TABLE {quoted_table} "
                    f"ADD COLUMN {_quote(connection, 'published_at')} TEXT"
                )
            )
        # A legacy due timestamp (or terminal timestamp) proves the binding was
        # exposed to recovery. Null/active rows remain unpublished and are
        # reconciled through the authoritative TaskNode startup path.
        connection.execute(
            text(
                f"UPDATE {quoted_table} "
                f"SET {_quote(connection, 'published_at')} = "
                f"COALESCE({_quote(connection, 'next_poll_at')}, "
                f"{_quote(connection, 'terminal_at')}, {_quote(connection, 'updated_at')}) "
                f"WHERE {_quote(connection, 'published_at')} IS NULL AND "
                f"({_quote(connection, 'next_poll_at')} IS NOT NULL OR "
                f"{_quote(connection, 'terminal_at')} IS NOT NULL)"
            )
        )


def _migrate_mcp_continuation_command_columns(engine: Engine) -> None:
    """Add the durable platform-continuation command state to existing stores."""

    with engine.begin() as connection:
        if "mcp_remote_task_outbox" not in set(inspect(connection).get_table_names()):
            return
        columns = {
            column["name"]
            for column in inspect(connection).get_columns("mcp_remote_task_outbox")
        }
        quoted_table = _quote(connection, "mcp_remote_task_outbox")
        nullable_text_columns = (
            "continuation_admitted_at",
            "continuation_dispatched_at",
            "continuation_status",
            "continuation_claim_owner",
            "continuation_claim_token",
            "continuation_lease_expires_at",
            "continuation_safe_error_code",
            "continuation_node_ids",
        )
        for column_name in nullable_text_columns:
            if column_name not in columns:
                connection.execute(
                    text(
                        f"ALTER TABLE {quoted_table} "
                        f"ADD COLUMN {_quote(connection, column_name)} TEXT"
                    )
                )
        if "continuation_revision" not in columns:
            connection.execute(
                text(
                    f"ALTER TABLE {quoted_table} "
                    f"ADD COLUMN {_quote(connection, 'continuation_revision')} "
                    "INTEGER NOT NULL DEFAULT 0"
                )
            )


def _migrate_mcp_rollout_metric_red_line(engine: Engine) -> None:
    """Add the closed red-line label and update the metric-series identity."""

    table_name = "mcp_rollout_metric_bucket"
    constraint_name = "uq_mcp_rollout_metric_series_bucket"
    with engine.begin() as connection:
        inspector = inspect(connection)
        if table_name not in set(inspector.get_table_names()):
            return
        existing_columns = {
            column["name"] for column in inspector.get_columns(table_name)
        }
        unique_constraints = inspector.get_unique_constraints(table_name)
        identity_has_red_line = any(
            constraint.get("name") == constraint_name
            and "red_line" in (constraint.get("column_names") or ())
            for constraint in unique_constraints
        )
        if "red_line" in existing_columns and identity_has_red_line:
            return

        target_table = SQLiteBase.metadata.tables[table_name]
        missing_required = [
            column.name
            for column in target_table.columns
            if column.name not in existing_columns
            and column.name != "red_line"
            and not column.nullable
            and column.default is None
            and column.server_default is None
        ]
        if missing_required:
            raise RuntimeError(
                "SQLite MCP rollout metric migration cannot rebuild table; "
                f"missing required columns: {', '.join(missing_required)}"
            )

        temp_table_name = "__maf_legacy_mcp_rollout_metric_bucket"
        quoted_table = _quote(connection, table_name)
        quoted_temp_table = _quote(connection, temp_table_name)
        old_row_count = _table_row_count(connection, table_name)
        connection.execute(text(f"DROP TABLE IF EXISTS {quoted_temp_table}"))
        connection.execute(
            text(f"ALTER TABLE {quoted_table} RENAME TO {quoted_temp_table}")
        )
        _drop_indexes_for_table(connection, temp_table_name)
        target_table.create(bind=connection, checkfirst=False)

        target_columns = [column.name for column in target_table.columns]
        insert_columns = ", ".join(
            _quote(connection, column_name) for column_name in target_columns
        )
        select_expressions: list[str] = []
        for column_name in target_columns:
            if column_name == "red_line":
                if column_name in existing_columns:
                    quoted_column = _quote(connection, column_name)
                    select_expressions.append(
                        f"COALESCE({quoted_column}, 'not_applicable')"
                    )
                else:
                    select_expressions.append("'not_applicable'")
                continue
            select_expressions.append(_quote(connection, column_name))
        connection.execute(
            text(
                f"INSERT INTO {quoted_table} ({insert_columns}) "
                f"SELECT {', '.join(select_expressions)} FROM {quoted_temp_table}"
            )
        )
        new_row_count = _table_row_count(connection, table_name)
        if new_row_count != old_row_count:
            raise RuntimeError(
                "SQLite MCP rollout metric migration copied "
                f"{new_row_count} rows; expected {old_row_count}"
            )
        connection.execute(text(f"DROP TABLE {quoted_temp_table}"))


def _migrate_mcp_rollout_evidence_attestation_columns(engine: Engine) -> None:
    """Preserve legacy evidence while adding persisted attestation material.

    Existing production rows remain nullable and therefore fail closed when
    independently revalidated. New writes enforce the source-specific contract
    in the repository; fresh databases also receive the table check constraint.
    """

    table_name = "mcp_rollout_evidence_snapshot"
    with engine.begin() as connection:
        if table_name not in set(inspect(connection).get_table_names()):
            return
        columns = {
            column["name"] for column in inspect(connection).get_columns(table_name)
        }
        quoted_table = _quote(connection, table_name)
        for column_name in ("attestation_key_id", "attestation_signature"):
            if column_name not in columns:
                connection.execute(
                    text(
                        f"ALTER TABLE {quoted_table} "
                        f"ADD COLUMN {_quote(connection, column_name)} TEXT"
                    )
                )


def _migrate_mcp_rollout_promotion_block_reasons(engine: Engine) -> None:
    """Expand the closed promotion-block reason set without losing history."""

    table_name = "mcp_rollout_promotion_block"
    required_reasons = {
        "attestation_missing",
        "attestation_invalid",
        "metric_series_missing",
        "metric_summary_mismatch",
        "safety_red_line_nonzero",
    }
    with engine.begin() as connection:
        if table_name not in set(inspect(connection).get_table_names()):
            return
        table_sql = connection.execute(
            text(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'table' AND name = :table_name"
            ),
            {"table_name": table_name},
        ).scalar_one()
        if all(reason in table_sql for reason in required_reasons):
            return

        target_table = SQLiteBase.metadata.tables[table_name]
        existing_columns = {
            column["name"] for column in inspect(connection).get_columns(table_name)
        }
        target_columns = [column.name for column in target_table.columns]
        missing_columns = set(target_columns) - existing_columns
        if missing_columns:
            raise RuntimeError(
                "SQLite MCP rollout promotion-block migration cannot rebuild table; "
                f"missing columns: {', '.join(sorted(missing_columns))}"
            )
        temp_table_name = "__maf_legacy_mcp_rollout_promotion_block"
        quoted_table = _quote(connection, table_name)
        quoted_temp_table = _quote(connection, temp_table_name)
        old_row_count = _table_row_count(connection, table_name)
        connection.execute(text(f"DROP TABLE IF EXISTS {quoted_temp_table}"))
        connection.execute(
            text(f"ALTER TABLE {quoted_table} RENAME TO {quoted_temp_table}")
        )
        _drop_indexes_for_table(connection, temp_table_name)
        target_table.create(bind=connection, checkfirst=False)
        rendered_columns = ", ".join(
            _quote(connection, column_name) for column_name in target_columns
        )
        connection.execute(
            text(
                f"INSERT INTO {quoted_table} ({rendered_columns}) "
                f"SELECT {rendered_columns} FROM {quoted_temp_table}"
            )
        )
        new_row_count = _table_row_count(connection, table_name)
        if new_row_count != old_row_count:
            raise RuntimeError(
                "SQLite MCP rollout promotion-block migration copied "
                f"{new_row_count} rows; expected {old_row_count}"
            )
        connection.execute(text(f"DROP TABLE {quoted_temp_table}"))


def _migrate_task_mcp_assignment_columns(engine: Engine) -> None:
    """Add nullable task-level MCP route assignment fields to existing stores."""

    with engine.begin() as connection:
        existing_tables = set(inspect(connection).get_table_names())
        if "task" not in existing_tables:
            return
        columns = {column["name"] for column in inspect(connection).get_columns("task")}
        quoted_table = _quote(connection, "task")
        column_types = {
            "mcp_execution_mode": "TEXT",
            "mcp_shadow_enabled": "BOOLEAN",
            "mcp_rollout_config_version": "TEXT",
            "mcp_route_reason_code": "TEXT",
            "mcp_rollout_mode": "TEXT",
        }
        for column_name, column_type in column_types.items():
            if column_name not in columns:
                connection.execute(
                    text(
                        f"ALTER TABLE {quoted_table} "
                        f"ADD COLUMN {_quote(connection, column_name)} {column_type}"
                    )
                )


def _migrate_task_mcp_route_reasons(engine: Engine) -> None:
    """Expand the closed task route reasons without changing persisted assignments."""

    table_name = "task"
    required_reasons = {
        "explicit_legacy_capability",
        "no_user_scoped_server",
        "user_server_rollout_unavailable",
    }
    with engine.begin() as connection:
        if table_name not in set(inspect(connection).get_table_names()):
            return
        table_sql = connection.execute(
            text(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'table' AND name = :table_name"
            ),
            {"table_name": table_name},
        ).scalar_one()
        if all(reason in table_sql for reason in required_reasons):
            return
        if "mcp_route_reason_code IN" not in table_sql:
            return

        target_table = SQLiteBase.metadata.tables[table_name]
        existing_columns = {
            column["name"] for column in inspect(connection).get_columns(table_name)
        }
        target_columns = [column.name for column in target_table.columns]
        missing_columns = set(target_columns) - existing_columns
        if missing_columns:
            raise RuntimeError(
                "SQLite task route-reason migration cannot rebuild table; "
                f"missing columns: {', '.join(sorted(missing_columns))}"
            )
        temp_table_name = "__maf_legacy_task_route_reasons"
        quoted_table = _quote(connection, table_name)
        quoted_temp_table = _quote(connection, temp_table_name)
        old_row_count = _table_row_count(connection, table_name)
        connection.execute(text("PRAGMA legacy_alter_table = ON"))
        try:
            connection.execute(text(f"DROP TABLE IF EXISTS {quoted_temp_table}"))
            connection.execute(
                text(f"ALTER TABLE {quoted_table} RENAME TO {quoted_temp_table}")
            )
            _drop_indexes_for_table(connection, temp_table_name)
            target_table.create(bind=connection, checkfirst=False)
            rendered_columns = ", ".join(
                _quote(connection, column_name) for column_name in target_columns
            )
            connection.execute(
                text(
                    f"INSERT INTO {quoted_table} ({rendered_columns}) "
                    f"SELECT {rendered_columns} FROM {quoted_temp_table}"
                )
            )
            new_row_count = _table_row_count(connection, table_name)
            if new_row_count != old_row_count:
                raise RuntimeError(
                    "SQLite task route-reason migration copied "
                    f"{new_row_count} rows; expected {old_row_count}"
                )
            connection.execute(text(f"DROP TABLE {quoted_temp_table}"))
        finally:
            connection.execute(text("PRAGMA legacy_alter_table = OFF"))


def _migrate_cp7_authority_columns(engine: Engine) -> None:
    """Add nullable bindings without inventing authority for legacy rows."""

    additions = {
        "mcp_call_record": {
            "server_config_version": "BIGINT",
        },
        "mcp_dispatch_resume_outbox": {
            "result_receipt_id": "TEXT",
            "completion_mode": "TEXT",
        },
        "mcp_terminal_result_receipt": {
            "safe_result_content_sha256": "TEXT",
            "safe_result_size_bytes": "BIGINT",
            "safe_result_store_kind": "TEXT",
        },
    }
    with engine.begin() as connection:
        tables = set(inspect(connection).get_table_names())
        for table_name, columns_to_add in additions.items():
            if table_name not in tables:
                continue
            existing = {
                column["name"] for column in inspect(connection).get_columns(table_name)
            }
            quoted_table = _quote(connection, table_name)
            for column_name, column_type in columns_to_add.items():
                if column_name not in existing:
                    connection.execute(
                        text(
                            f"ALTER TABLE {quoted_table} ADD COLUMN "
                            f"{_quote(connection, column_name)} {column_type}"
                        )
                    )


def _ensure_cp7_mutation_triggers(engine: Engine) -> None:
    """Install additive fail-closed mutation guards for CP7 evidence tables."""

    append_only_tables = (
        "mcp_cp7_safety_ledger",
        "mcp_cp7_ready_epoch_event",
        "mcp_terminal_result_receipt",
        "mcp_no_server_convergence_receipt",
        "mcp_legacy_retirement_evidence",
        "mcp_legacy_retirement_receipt",
    )
    with engine.begin() as connection:
        tables = set(inspect(connection).get_table_names())
        if "mcp_cp7_safety_ledger" in tables:
            quoted_ledger = _quote(connection, "mcp_cp7_safety_ledger")
            connection.execute(
                text(
                    "CREATE TRIGGER IF NOT EXISTS "
                    f"{_quote(connection, 'trg_mcp_cp7_safety_attestation_window')} "
                    f"BEFORE INSERT ON {quoted_ledger} "
                    "WHEN NEW.record_kind = 'attestation' AND COALESCE(("
                    "NEW.bucket_started_at IS NOT NULL AND "
                    "NEW.bucket_ended_at IS NOT NULL AND "
                    "(substr(NEW.bucket_started_at, -1) = 'Z' OR "
                    "substr(NEW.bucket_started_at, -6) = '+00:00') AND "
                    "(substr(NEW.bucket_ended_at, -1) = 'Z' OR "
                    "substr(NEW.bucket_ended_at, -6) = '+00:00') AND "
                    "strftime('%S', NEW.bucket_started_at) = '00' AND "
                    "strftime('%f', NEW.bucket_started_at) = '00.000' AND "
                    "strftime('%S', NEW.bucket_ended_at) = '00' AND "
                    "strftime('%f', NEW.bucket_ended_at) = '00.000' AND "
                    "unixepoch(NEW.bucket_ended_at) - "
                    "unixepoch(NEW.bucket_started_at) = 60), 0) = 0 "
                    "BEGIN SELECT RAISE(ABORT, 'cp7_attestation_window_invalid'); END"
                )
            )
        for table_name in append_only_tables:
            if table_name not in tables:
                continue
            for operation in ("UPDATE", "DELETE"):
                trigger_name = f"trg_{table_name}_reject_{operation.lower()}"
                connection.execute(
                    text(
                        f"CREATE TRIGGER IF NOT EXISTS {_quote(connection, trigger_name)} "
                        f"BEFORE {operation} ON {_quote(connection, table_name)} "
                        "BEGIN SELECT RAISE(ABORT, 'cp7_append_only_violation'); END"
                    )
                )

        if "mcp_cp7_candidate_guard" not in tables:
            return
        quoted_guard = _quote(connection, "mcp_cp7_candidate_guard")
        connection.execute(
            text(
                "CREATE TRIGGER IF NOT EXISTS "
                f"{_quote(connection, 'trg_mcp_cp7_candidate_guard_reject_delete')} "
                f"BEFORE DELETE ON {quoted_guard} "
                "BEGIN SELECT RAISE(ABORT, 'cp7_candidate_guard_delete'); END"
            )
        )
        connection.execute(
            text(
                "CREATE TRIGGER IF NOT EXISTS "
                f"{_quote(connection, 'trg_mcp_cp7_candidate_guard_monotonic')} "
                f"BEFORE UPDATE ON {quoted_guard} WHEN "
                "NEW.candidate_id IS NOT OLD.candidate_id OR "
                "NEW.created_at IS NOT OLD.created_at OR "
                "(OLD.invalid_latched = true AND ("
                "NEW.invalid_latched <> true OR "
                "NEW.first_invalid_record_id IS NOT OLD.first_invalid_record_id OR "
                "NEW.first_invalid_reason IS NOT OLD.first_invalid_reason OR "
                "NEW.first_invalid_at IS NOT OLD.first_invalid_at)) OR "
                "(OLD.invalid_latched = false AND NEW.invalid_latched = false AND ("
                "NEW.first_invalid_record_id IS NOT NULL OR "
                "NEW.first_invalid_reason IS NOT NULL OR NEW.first_invalid_at IS NOT NULL)) OR "
                "(OLD.invalid_latched = false AND NEW.invalid_latched = true AND ("
                "NEW.first_invalid_record_id IS NULL OR "
                "NEW.first_invalid_reason IS NULL OR NEW.first_invalid_at IS NULL)) "
                "BEGIN SELECT RAISE(ABORT, 'cp7_candidate_guard_monotonic'); END"
            )
        )
        if "user_mcp_owner_mutation_guard" in tables:
            quoted_owner_guard = _quote(
                connection, "user_mcp_owner_mutation_guard"
            )
            connection.execute(
                text(
                    "CREATE TRIGGER IF NOT EXISTS "
                    f"{_quote(connection, 'trg_user_mcp_owner_guard_reject_delete')} "
                    f"BEFORE DELETE ON {quoted_owner_guard} "
                    "BEGIN SELECT RAISE(ABORT, 'user_mcp_owner_guard_delete'); END"
                )
            )
            connection.execute(
                text(
                    "CREATE TRIGGER IF NOT EXISTS "
                    f"{_quote(connection, 'trg_user_mcp_owner_guard_monotonic')} "
                    f"BEFORE UPDATE ON {quoted_owner_guard} WHEN "
                    "NEW.owner_user_id IS NOT OLD.owner_user_id OR "
                    "NEW.created_at IS NOT OLD.created_at OR "
                    "NEW.revision <> OLD.revision + 1 "
                    "BEGIN SELECT RAISE(ABORT, 'user_mcp_owner_guard_monotonic'); END"
                )
            )

        if "mcp_execution_terminal_projection" in tables:
            quoted_projection = _quote(
                connection, "mcp_execution_terminal_projection"
            )
            connection.execute(
                text(
                    "CREATE TRIGGER IF NOT EXISTS "
                    f"{_quote(connection, 'trg_mcp_terminal_projection_reject_delete')} "
                    f"BEFORE DELETE ON {quoted_projection} "
                    "BEGIN SELECT RAISE(ABORT, 'mcp_terminal_projection_delete'); END"
                )
            )
            connection.execute(
                text(
                    "CREATE TRIGGER IF NOT EXISTS "
                    f"{_quote(connection, 'trg_mcp_terminal_projection_monotonic')} "
                    f"BEFORE UPDATE ON {quoted_projection} WHEN NOT ("
                    "OLD.status = 'unknown' AND OLD.revision = 0 AND "
                    "NEW.status = 'late_result_resolved' AND NEW.revision = 1 AND "
                    "NEW.projection_id IS OLD.projection_id AND "
                    "NEW.owner_user_id IS OLD.owner_user_id AND "
                    "NEW.conversation_id IS OLD.conversation_id AND "
                    "NEW.intent_id IS OLD.intent_id AND NEW.call_id IS OLD.call_id AND "
                    "NEW.task_id IS OLD.task_id AND NEW.node_id IS OLD.node_id AND "
                    "NEW.unknown_event_id IS OLD.unknown_event_id AND "
                    "NEW.task_failed_event_id IS OLD.task_failed_event_id AND "
                    "NEW.unknown_terminal_at IS OLD.unknown_terminal_at AND "
                    "NEW.created_at IS OLD.created_at) "
                    "BEGIN SELECT RAISE(ABORT, 'mcp_terminal_projection_monotonic'); END"
                )
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
