from __future__ import annotations

import json
from typing import Any, Mapping

from .llm_utils import json_ready


DEFAULT_SUMMARY_PREVIEW_ROWS = 20
DEFAULT_MAX_SUMMARY_CHARS = 800


def build_sql_generation_prompt_payload(
    context: Mapping[str, Any],
    *,
    task_meta: Mapping[str, Any] | None = None,
    guard_constraints: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "task_meta": {
            "node_name": "sql_query.sql_generate",
            **dict(task_meta or {}),
        },
        "route_context": {
            "route_id": context.get("route_id"),
            "schema_profile_id": context.get("schema_profile_id"),
            "allowed_tables": list(context.get("allowed_tables", [])),
            "sql_policy_profile": context.get("sql_policy_profile"),
        },
        "schema_context": {
            "selected_tables": list(context.get("selected_tables", [])),
            "selected_columns": {
                str(table): list(columns)
                for table, columns in dict(context.get("selected_columns", {})).items()
            },
            "selected_column_details": json_ready(dict(context.get("selected_column_details", {}))),
            "join_hints": json_ready(list(context.get("join_hints", []))),
            "context_summary": context.get("context_summary"),
        },
        "guard_constraints": {
            "readonly_only": True,
            "single_statement_only": True,
            "require_limit_for_non_aggregate_query": True,
            "max_limit": 200,
            "allowed_statement_types": ["SELECT", "WITH_SELECT"],
            **dict(guard_constraints or {}),
        },
        "user_question": context.get("user_question"),
        "output_contract": {
            "format": "JSON object only",
            "mode": "answer | clarify | reject",
            "answer_required_fields": ["mode", "route_id", "schema_profile_id", "sql", "tables_used", "columns_used", "column_types_used"],
            "clarify_required_fields": ["mode", "clarifying_question"],
            "reject_required_fields": ["mode", "reject_reason", "supported_scope_hint"],
        },
    }


def build_sql_generation_prompt(
    context: Mapping[str, Any],
    *,
    task_meta: Mapping[str, Any] | None = None,
    guard_constraints: Mapping[str, Any] | None = None,
) -> str:
    payload = build_sql_generation_prompt_payload(context, task_meta=task_meta, guard_constraints=guard_constraints)
    return (
        "你是 SQLQuery 的 SQL 草案生成器。只能根据输入中裁剪后的 schema_context 生成只读 MySQL SQL。\n"
        "字段名和字段类型必须严格来自 schema_context.selected_column_details；"
        "column_types_used 必须逐项回填 schema 中的 sql_type。"
        "安全要求：readonly_only=true；只允许单条 SELECT 或 WITH...SELECT；非聚合查询必须包含 LIMIT；"
        "只能使用 allowed_tables / selected_tables 中的表；多表 JOIN 只能使用 join_hints。\n"
        "如果信息不足，返回 mode=clarify 且只问一个最关键问题；如果超出支持范围，返回 mode=reject。\n"
        "输出必须是 JSON，不要输出 Markdown，不要解释。mode 只能是 answer | clarify | reject。\n"
        "输入如下：\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str)}"
    )


def build_result_summary_prompt_payload(
    execute_context: Mapping[str, Any],
    *,
    question_context: Mapping[str, Any] | None = None,
    max_preview_rows: int = DEFAULT_SUMMARY_PREVIEW_ROWS,
    max_summary_chars: int = DEFAULT_MAX_SUMMARY_CHARS,
) -> dict[str, Any]:
    rows = list(execute_context.get("rows", []))
    rows_preview = [json_ready(row) for row in rows[:max_preview_rows]]
    row_count = int(execute_context.get("row_count", len(rows)))
    preview_row_count = len(rows_preview)
    truncated = row_count > preview_row_count or len(rows) > preview_row_count
    question = dict(question_context or {})
    sql = execute_context.get("sql") or question.get("sql")
    return {
        "task_meta": {"node_name": "sql_query.result_summarize"},
        "question_context": {
            "user_question": question.get("user_question"),
            "route_id": question.get("route_id"),
            "schema_profile_id": question.get("schema_profile_id"),
            "sql": sql,
            "generation_source": question.get("generation_source"),
        },
        "result_context": {
            "columns": list(execute_context.get("columns", [])),
            "row_count": row_count,
            "rows_preview": rows_preview,
            "preview_row_count": preview_row_count,
            "truncated": truncated,
        },
        "summary_policy": {
            "language": "zh-CN",
            "do_not_fabricate": True,
            "mention_truncation_when_truncated": True,
            "max_summary_chars": max_summary_chars,
        },
        "output_contract": {
            "format": "JSON object only",
            "required_fields": ["summary"],
            "optional_fields": ["highlights", "caveats", "row_count", "preview_row_count", "truncated"],
        },
    }


def build_result_summary_prompt(
    execute_context: Mapping[str, Any],
    *,
    question_context: Mapping[str, Any] | None = None,
    max_preview_rows: int = DEFAULT_SUMMARY_PREVIEW_ROWS,
    max_summary_chars: int = DEFAULT_MAX_SUMMARY_CHARS,
) -> str:
    payload = build_result_summary_prompt_payload(
        execute_context,
        question_context=question_context,
        max_preview_rows=max_preview_rows,
        max_summary_chars=max_summary_chars,
    )
    return (
        "你是 SQLQuery 的结果摘要器。只能解释已经执行完成的 SQL 查询结果。\n"
        "不要重新生成 SQL，不要要求补查数据库，不要根据字段名或常识编造结果集中不存在的信息。\n"
        "如果 result_context.truncated=true，摘要中必须说明仅基于 rows_preview 预览。请用中文输出。\n"
        "输出必须是 JSON，不要输出 Markdown；summary 是唯一稳定必填的用户可见主字段。不要编造。\n"
        "输入如下：\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str)}"
    )
