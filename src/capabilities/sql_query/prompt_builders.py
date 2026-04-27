from __future__ import annotations

import json
from typing import Any, Mapping

from .llm_utils import json_ready


DEFAULT_FILTER_CANDIDATE_ROWS = 200


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
        "first_principles_need_inference": {
            "principle": "不要假定用户知道该选择哪个库、哪张表或哪种技术路径；先从用户自然语言里的实体、业务目标和可用 schema 出发，推断最有帮助且安全的最小查询。",
            "default_behavior": "如果问题宽泛但仍在 SQLQuery 支持范围内，优先返回可验证的概览查询，而不是立刻要求用户补充库名/路径。",
            "clarify_only_when": "只有在缺少实体或安全生成只读 SQL 不可能时，才使用 mode=clarify，并且只问一个最关键问题。",
            "broad_variety_lookup": "当 route_id=variety_overview 时，应把它理解为品种综合概览：在 allowed_tables 内同时覆盖审定品种信息与基因型基础/籼粳成分信息；必要时可用 UNION ALL 保持单条只读语句。",
        },
        "variety_name_matching_policy": {
            "rule": "当按品种名称 / variety_name 过滤时，必须使用 LIKE 通配匹配，不得使用严格等值条件。",
            "preferred_pattern": "variety_name LIKE '%关键词%'",
            "forbidden_pattern": "variety_name = '关键词'",
            "series_or_contains_pattern": "当用户说“X系列”“名字里带X”“名称中包含X”时，X 就是 LIKE 关键词，应生成 variety_name LIKE '%X%'。",
            "reason": "业务用户常输入简称、部分名称或带后缀的品种名，严格等值容易漏查。",
        },
        "business_output_defaults": {
            "approval_variety_single_detail": {
                "when": "route_id=approval_variety_db 且用户查询单个品种或要求详细/全部信息",
                "prefer_columns": [
                    "year",
                    "approval_num",
                    "crop_name",
                    "variety_name",
                    "applicant",
                    "breeder",
                    "variety_source",
                    "characteristics",
                    "yield_performance",
                    "cultivation_tips",
                    "suitable_area",
                    "approval_opinion",
                ],
            },
            "approval_variety_list": {
                "when": "route_id=approval_variety_db 且用户查询列表/近几年/有哪些",
                "prefer_columns": ["year", "approval_num", "crop_name", "variety_name", "applicant", "breeder", "suitable_area"],
            },
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
        "你必须用第一性原理理解用户需求：不要假定用户知道该选择哪个库、哪张表或哪条技术路径；"
        "先识别自然语言里的实体、用户真正想解决的业务问题、以及当前 schema 能安全回答的最小有用结果。\n"
        "如果用户问题宽泛但仍可安全查询，优先生成概览型 SQL；不要因为用户没有说清库名/路径就直接 clarify。"
        "当按品种名称或 variety_name 过滤时，必须使用 LIKE 通配匹配（建议 LIKE '%关键词%'），不得使用严格等值条件 variety_name = '关键词'。"
        "当用户说“X系列”“名字里带X”或“名称中包含X”时，X 就是品种名 LIKE 关键词，应该生成 variety_name LIKE '%X%'。"
        "当 route_id=approval_variety_db 且用户查单个品种或要求详细/全部信息时，默认生成业务详情查询，"
        "优先包含审定编号、年份、作物、品种名、申请者、育种者、品种来源、特征特性、产量表现、栽培技术要点、适种区域、审定意见等字段；"
        "当用户查列表时，优先返回年份、审定编号、品种名、申请者、育种者、适种区域等列表字段。"
        "只有在缺少实体或安全生成只读 SQL 不可能时，才返回 mode=clarify。\n"
        "字段名和字段类型必须严格来自 schema_context.selected_column_details；"
        "column_types_used 必须逐项回填 schema 中的 sql_type。"
        "安全要求：readonly_only=true；只允许单条 SELECT 或 WITH...SELECT；非聚合查询必须包含 LIMIT；"
        "只能使用 allowed_tables / selected_tables 中的表；多表 JOIN 只能使用 join_hints。\n"
        "如果信息不足，返回 mode=clarify 且只问一个最关键问题；如果超出支持范围，返回 mode=reject。\n"
        "输出必须是 JSON，不要输出 Markdown，不要解释。mode 只能是 answer | clarify | reject。\n"
        "输入如下：\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str)}"
    )


def build_result_filtering_prompt_payload(
    execute_context: Mapping[str, Any],
    *,
    question_context: Mapping[str, Any] | None = None,
    max_candidate_rows: int = DEFAULT_FILTER_CANDIDATE_ROWS,
) -> dict[str, Any]:
    rows = list(execute_context.get("rows", []))
    candidate_rows = [
        {
            "row_index": index,
            "values": json_ready(row),
        }
        for index, row in enumerate(rows[:max_candidate_rows])
    ]
    row_count = int(execute_context.get("row_count", len(rows)))
    candidate_row_count = len(candidate_rows)
    truncated = row_count > candidate_row_count or len(rows) > candidate_row_count
    question = dict(question_context or {})
    sql = execute_context.get("sql") or question.get("sql")
    return {
        "task_meta": {"node_name": "sql_query.result_filtering"},
        "question_context": {
            "user_question": question.get("user_question"),
            "route_id": question.get("route_id"),
            "schema_profile_id": question.get("schema_profile_id"),
            "sql": sql,
            "generation_source": question.get("generation_source"),
        },
        "result_context": {
            "columns": list(execute_context.get("columns", [])),
            "source_row_count": row_count,
            "candidate_rows": candidate_rows,
            "candidate_row_count": candidate_row_count,
            "truncated": truncated,
        },
        "filtering_policy": {
            "purpose": "SQL 生成阶段使用 LIKE 是为了扩大候选集、避免品种名称缺字或多字导致漏查；本节点负责从候选结果中筛掉不符合用户真实需求的行。",
            "do_not_summarize": True,
            "only_use_candidate_rows": True,
            "do_not_fabricate": True,
            "keep_all_matching_rows": True,
            "remove_clearly_unrelated_rows": True,
            "numbered_variety_exactness": "当用户明确查询带数字编号的单个品种（例如 龙粳18）时，只保留品种名规范化后等于该编号或该编号+“号”的行；不得保留继续追加数字/字母/后缀的其他品种（例如 龙粳1836、龙粳1823 不是 龙粳18）。",
            "conservative_when_uncertain": "如果某行只是简称、别名、缺字或多字但仍可能对应用户需求，可以保留；如果名称明显不是同一品种或实体，应移除。",
            "empty_result_allowed": True,
        },
        "output_contract": {
            "format": "JSON object only",
            "required_fields": ["keep_row_indexes"],
            "optional_fields": ["filter_reason"],
            "keep_row_indexes": "0-based row_index values from result_context.candidate_rows; do not return row objects.",
        },
    }


def build_result_filtering_prompt(
    execute_context: Mapping[str, Any],
    *,
    question_context: Mapping[str, Any] | None = None,
    max_candidate_rows: int = DEFAULT_FILTER_CANDIDATE_ROWS,
) -> str:
    payload = build_result_filtering_prompt_payload(
        execute_context,
        question_context=question_context,
        max_candidate_rows=max_candidate_rows,
    )
    return (
        "你是 SQLQuery 的结果筛选器，不是摘要器。你只能筛选已经执行完成的 SQL 查询结果，最后由系统返回筛选后的表格。\n"
        "SQL 生成阶段故意用 LIKE 匹配品种名，是为了先召回候选行；现在你要结合用户问题判断 candidate_rows 中哪条或哪几条真正符合需求。\n"
        "不要总结，不要改写 SQL，不要要求补查数据库，不要根据字段名或常识编造候选集中不存在的行。\n"
        "特别注意：当用户查询的是带数字编号的单个品种（如“龙粳18”），只保留“龙粳18”和“龙粳18号”这类规范化等值名称；"
        "“龙粳1836”“龙粳1823”“龙粳1851”等是在编号后继续追加数字的其他品种，必须排除。\n"
        "如果某行名称明显不是用户要查的品种/实体，把它从 keep_row_indexes 中排除；如果名称只是简称、别名、缺字或多字但仍可能对应，可以保留。\n"
        "输出必须是 JSON，不要输出 Markdown；必须返回 keep_row_indexes，值只能是 candidate_rows 中已有 row_index 的数组。\n"
        "输入如下：\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str)}"
    )
