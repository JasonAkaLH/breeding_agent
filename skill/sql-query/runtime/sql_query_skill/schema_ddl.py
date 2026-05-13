from __future__ import annotations

from typing import Any, Mapping, Sequence


def render_mysql_schema_ddl(
    schema_metadata: Mapping[str, Any],
    table_names: Sequence[str],
    *,
    selected_columns: Mapping[str, Sequence[str]] | None = None,
) -> str:
    """Render a MySQL DDL-style schema snippet from schema metadata for LLM prompts.

    The project keeps schema in YAML metadata rather than raw SQL files. SQLQuery's
    SQL-generation prompt, however, works better with the DDL-shaped context used by
    the legacy sub-agent, so this renderer converts only the already selected
    route/table/column scope into a compact CREATE TABLE block.
    """

    tables = schema_metadata.get("tables", {})
    if not isinstance(tables, Mapping):
        return ""

    blocks: list[str] = []
    for table_name in table_names:
        table_key = str(table_name)
        table_meta = tables.get(table_key, {})
        if not isinstance(table_meta, Mapping):
            continue
        block = _render_table_ddl(
            table_key,
            table_meta,
            selected_columns=selected_columns.get(table_key) if selected_columns is not None else None,
            selected_table_names={str(name) for name in table_names},
        )
        if block:
            blocks.append(block)

    return "\n\n".join(blocks).strip()


def _render_table_ddl(
    table_name: str,
    table_meta: Mapping[str, Any],
    *,
    selected_columns: Sequence[str] | None,
    selected_table_names: set[str],
) -> str:
    columns_meta = table_meta.get("columns", {})
    if not isinstance(columns_meta, Mapping):
        return ""

    column_names = [str(name) for name in (selected_columns if selected_columns is not None else columns_meta.keys())]
    column_names = [name for name in column_names if isinstance(columns_meta.get(name), Mapping)]
    if not column_names:
        return ""

    primary_key = [str(column) for column in table_meta.get("primary_key", [])]
    primary_key_in_scope = [column for column in primary_key if column in column_names]
    definitions: list[str] = []
    for column_name in column_names:
        column_meta = columns_meta.get(column_name, {})
        if not isinstance(column_meta, Mapping):
            continue
        definitions.append(_render_column_definition(column_name, column_meta, is_primary=column_name in primary_key_in_scope))

    if primary_key_in_scope:
        keys = ", ".join(f"`{column}`" for column in primary_key_in_scope)
        definitions.append(f"  PRIMARY KEY ({keys})")

    definitions.extend(_render_foreign_key_definitions(table_name, table_meta, column_names, selected_table_names))
    if not definitions:
        return ""

    table_comment = _escape_comment(str(table_meta.get("description", "")))
    comment_suffix = f" COMMENT = '{table_comment}'" if table_comment else ""
    return "\n".join(
        [
            "-- ----------------------------",
            f"-- 表结构：{table_name}",
            "-- ----------------------------",
            f"CREATE TABLE `{table_name}`  (",
            ",\n".join(definitions),
            f"){comment_suffix};",
        ]
    )


def _render_column_definition(column_name: str, column_meta: Mapping[str, Any], *, is_primary: bool) -> str:
    sql_type = str(column_meta.get("sql_type") or "text").strip() or "text"
    nullability = "NOT NULL" if is_primary else "DEFAULT NULL"
    comment = _escape_comment(str(column_meta.get("description", "")))
    comment_clause = f" COMMENT '{comment}'" if comment else ""
    return f"  `{column_name}` {sql_type} {nullability}{comment_clause}"


def _render_foreign_key_definitions(
    table_name: str,
    table_meta: Mapping[str, Any],
    column_names: Sequence[str],
    selected_table_names: set[str],
) -> list[str]:
    column_set = set(column_names)
    definitions: list[str] = []
    for foreign_key in table_meta.get("foreign_keys", []):
        if not isinstance(foreign_key, Mapping):
            continue
        column = str(foreign_key.get("column") or "")
        ref_table = str(foreign_key.get("ref_table") or "")
        ref_column = str(foreign_key.get("ref_column") or "")
        if not column or not ref_table or not ref_column:
            continue
        if column not in column_set or ref_table not in selected_table_names:
            continue
        constraint_name = f"fk_{table_name}_{column}"
        definitions.append(
            f"  CONSTRAINT `{constraint_name}` FOREIGN KEY (`{column}`) "
            f"REFERENCES `{ref_table}` (`{ref_column}`)"
        )
    return definitions


def _escape_comment(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")
