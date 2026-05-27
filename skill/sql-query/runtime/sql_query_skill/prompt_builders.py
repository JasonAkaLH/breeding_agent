from __future__ import annotations

import json
from typing import Any, Mapping

from .llm_utils import json_ready


_CROP_TABLE_MAPPING_TEXT = """'玉米' -> corn_varieties;
     '水稻' -> rice_varieties;
     '棉花' -> cotton_varieties;
     '大豆' -> soybean_varieties;
     '小麦' -> wheat_varieties;"""


def build_sql_generation_prompt_payload(
    context: Mapping[str, Any],
    *,
    task_meta: Mapping[str, Any] | None = None,
    guard_constraints: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    database_schema = _database_schema(context)
    return {
        "task_meta": {
            "stage": "sql_generate",
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
            "database_schema": database_schema,
        },
        "guard_constraints": {
            "readonly_only": True,
            "single_statement_only": True,
            "limit_policy": "不要自动添加 LIMIT；只有用户明确要求前 N 条、限制条数或分页时才生成 LIMIT。",
            "table_scope_policy": "只能引用 SQL 生成提示词中 database_schema 已注入的表。",
            "allowed_statement_types": ["SELECT", "WITH_SELECT"],
            **dict(guard_constraints or {}),
        },
        "first_principles_need_inference": {
            "principle": "不要假定用户知道该选择哪个库、哪张表或哪种技术路径；先从用户自然语言里的实体、业务目标和可用 schema 出发，推断最有帮助且安全的最小查询。",
            "default_behavior": "SQL 生成阶段只处理上游已解析出的单一数据库路由；如果用户没有命中路由关键词，上游必须先由 LLM 路由或澄清确定审定品种库 / 基因型数据库。",
            "clarify_only_when": "只有在缺少实体或安全生成只读 SQL 不可能时，才应由上游路由 / schema 节点澄清，并且只问一个最关键问题。",
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
            "format": "仅 SQL",
            "llm_output": "只输出 SQL 查询语句，不输出 JSON、Markdown 或解释。",
            "sql_shape": "单条只读 SELECT 或 WITH...SELECT；不要自动添加 LIMIT。",
        },
    }


def build_sql_generation_prompt(
    context: Mapping[str, Any],
    *,
    task_meta: Mapping[str, Any] | None = None,
    guard_constraints: Mapping[str, Any] | None = None,
) -> str:
    payload = build_sql_generation_prompt_payload(context, task_meta=task_meta, guard_constraints=guard_constraints)
    route_id = str(context.get("route_id") or "")
    if route_id == "genotype_db":
        return _build_gene_sql_generation_prompt(payload)
    if route_id == "approval_variety_db":
        return _build_varieties_sql_generation_prompt(payload)
    return _build_general_sql_generation_prompt(payload)


def _build_varieties_sql_generation_prompt(payload: Mapping[str, Any]) -> str:
    query = str(payload.get("user_question") or "")
    schema_context = dict(payload.get("schema_context", {}))
    route_context = dict(payload.get("route_context", {}))
    database_schema = str(schema_context.get("database_schema") or "")
    return f"""
    生成一个SQL查询来回答这个问题：{query}
    当前阶段：sql_generate
    当前SQLQuery路由：{route_context.get("route_id")}；schema_profile：{route_context.get("schema_profile_id")}
    
    ======================================================================================================================================================================
    ## 以下是数据库结构
    ```sql
    {database_schema}
    ```

    ======================================================================================================================================================================
    请注意，以下SQL查询语句中，表名和字段名都是小写的，请注意大小写。

    ## 限制
    - 你只能使用当前注入到 `database_schema` 中的表结构生成 SQL；不要引用未出现在 `database_schema` 里的表。
    - 如果当前注入的是单个作物表，则只在该作物表范围内查询；如果当前注入了多个作物表，才允许做多表或跨作物查询。
    - 作物与品种表的对应关系如下：
     {_CROP_TABLE_MAPPING_TEXT}
    - **注意！！如果用户的问题里没有明确作物，且当前注入了多个品种表，你可以在这些已注入的品种表范围内查询。**
    - **注意！！在多表查询或者跨表查询时，应当注意每个表的字段总数以及字段名是不一样的，注意生成SQL语句时的逻辑。**
    - **注意！！你需要输出字段的注释作为列名，而不是字段名！！！**
    - 只能生成单条只读 SELECT 或 WITH...SELECT SQL，禁止生成写操作、DDL、多语句或跨库访问。
    - 不要自动添加 LIMIT；只有用户明确要求前 N 条、限制条数或分页时才生成 LIMIT。
    ## 查询技巧
    - SELECT 投影字段、WHERE 过滤字段和排序字段由你根据 `database_schema` 中的字段注释自行选择；不要依赖系统预先挑字段。
    - 如果用户提到地区、省份、适合/适宜种植、适种区域，应优先使用注释为“适种区域”的字段做 LIKE 过滤，并把相关字段放入 SELECT。
    - 在查询品种表的`applicant`和`breeder`字段时，尽量使用包含关系，即字段中是否包含所查询的值。而不是等于关系。
    - 查询品种名称的时候，必须使用 LIKE 包含关系召回候选；同时要注意分辨名称很相近的品种，例如：“登海618”与“登海6188”，“东单1331”与“东单1331D”和“东单1331K”，等等；这些都是不同的品种，应注意区分！！！
    - 当用户在询问某特定品种但没有给出属于什么作物时，只能在当前注入的品种表范围内生成 SQL；如果 `database_schema` 已经收敛到单个作物表，不要再跨到其他作物表。
    - 如果用户查询中有多个品种分属不同作物，那你只要输出这些表中共同拥有的列；每个表特有的列不要查询。

    **注意！！你只需要输出SQL语句，不要输出其他任何内容！！！**
    以下SQL查询最能回答问题 {query}：
    ```sql
    """.strip()


def _build_gene_sql_generation_prompt(payload: Mapping[str, Any]) -> str:
    query = str(payload.get("user_question") or "")
    schema_context = dict(payload.get("schema_context", {}))
    route_context = dict(payload.get("route_context", {}))
    database_schema = str(schema_context.get("database_schema") or "")
    join_hints = _format_join_hints(schema_context.get("join_hints"))
    return f"""
    生成一个SQL查询来回答这个问题：{query}
    当前阶段：sql_generate
    当前SQLQuery路由：{route_context.get("route_id")}；schema_profile：{route_context.get("schema_profile_id")}
    
    ======================================================================================================================================================================
    ## 以下是数据库结构

    {database_schema}

    ======================================================================================================================================================================
    ## 连接关系

    {join_hints}

    ## 查询技巧
    - 多表查询时，你需要根据表的连接关系，生成SQL查询语句。
    - SELECT 投影字段、WHERE 过滤字段和排序字段由你根据数据库结构中的字段注释自行选择。
    
    ## 限制
    请注意，以下SQL查询语句中，表名和字段名都是小写的，请注意大小写。
    请注意，查询rice_comp时，同样的variety_name可能有多条记录，当你输出时，需要带有variety_name和的variety_id值。
    只能生成单条只读 SELECT 或 WITH...SELECT SQL，禁止生成写操作、DDL、多语句或跨库访问。
    不要自动添加 LIMIT；只有用户明确要求前 N 条、限制条数或分页时才生成 LIMIT。

    ## 品种的搜索：
    - 在搜索'variety'表的'variety_name'字段时，尽量使用包含关系（建议 LIKE '%关键词%'），即字段中是否包含所查询的值。而不是等于关系。
    - 在搜索'variety_genotype'表的'variety_id'字段时，尽量使用等于关系，即字段中是否等于所查询的值。而不是包含关系。
    - 在搜索'qtn'表的'qtn_id'字段时，尽量使用等于关系，即字段中是否等于所查询的值。而不是包含关系。

    **注意！！你只需要输出SQL语句，不要输出其他任何内容！！！**
    以下SQL查询最能回答问题 {query}：
    ```sql
    """.strip()


def _build_general_sql_generation_prompt(payload: Mapping[str, Any]) -> str:
    query = str(payload.get("user_question") or "")
    schema_context = dict(payload.get("schema_context", {}))
    route_context = dict(payload.get("route_context", {}))
    database_schema = str(schema_context.get("database_schema") or "")
    join_hints = _format_join_hints(schema_context.get("join_hints"))
    return f"""
    生成一个SQL查询来回答这个问题：{query}
    当前阶段：sql_generate
    当前SQLQuery路由：{route_context.get("route_id")}；schema_profile：{route_context.get("schema_profile_id")}
    
    ======================================================================================================================================================================
    ## 以下是数据库结构
    ```sql
    {database_schema}
    ```

    ======================================================================================================================================================================
    ## 连接关系
    {join_hints}

    ## 限制
    - 你只能使用当前注入到 `database_schema` 中的表结构生成 SQL；不要引用未出现在 `database_schema` 里的表。
    - 只能生成单条只读 SELECT 或 WITH...SELECT SQL，禁止生成写操作、DDL、多语句或跨库访问。
    - 不要自动添加 LIMIT；只有用户明确要求前 N 条、限制条数或分页时才生成 LIMIT。
    - SELECT 投影字段、WHERE 过滤字段和排序字段由你根据字段注释自行选择。
    - 当按品种名称或 variety_name 过滤时，必须使用 LIKE 包含关系，不要使用严格等值。

    **注意！！你只需要输出SQL语句，不要输出其他任何内容！！！**
    以下SQL查询最能回答问题 {query}：
    ```sql
    """.strip()


def _database_schema(context: Mapping[str, Any]) -> str:
    explicit = context.get("schema_ddl") or context.get("ddl_schema_context") or context.get("database_schema")
    if explicit:
        return str(explicit).strip()
    return _render_schema_from_column_details(context)


def _render_schema_from_column_details(context: Mapping[str, Any]) -> str:
    details = dict(context.get("selected_column_details", {}))
    selected_columns = dict(context.get("selected_columns", {}))
    selected_tables = [str(table) for table in list(context.get("selected_tables", []))]
    blocks: list[str] = []
    for table in selected_tables:
        raw_columns = details.get(table) or [
            {"name": column, "sql_type": "text", "description": ""}
            for column in list(selected_columns.get(table, []))
        ]
        definitions: list[str] = []
        for column in raw_columns:
            if not isinstance(column, Mapping):
                continue
            name = str(column.get("name") or "")
            if not name:
                continue
            sql_type = str(column.get("sql_type") or "text")
            description = str(column.get("description") or "")
            comment = f" COMMENT '{description}'" if description else ""
            definitions.append(f"  `{name}` {sql_type} DEFAULT NULL{comment}")
        if not definitions:
            continue
        blocks.append(
            "\n".join(
                [
                    "-- ----------------------------",
                    f"-- 表结构：{table}",
                    "-- ----------------------------",
                    f"CREATE TABLE `{table}`  (",
                    ",\n".join(definitions),
                    ");",
                ]
            )
        )
    return "\n\n".join(blocks).strip()


def _format_join_hints(join_hints: Any) -> str:
    lines: list[str] = []
    for hint in list(join_hints or []):
        if not isinstance(hint, Mapping):
            continue
        left_table = str(hint.get("left_table") or "")
        left_column = str(hint.get("left_column") or "")
        right_table = str(hint.get("right_table") or "")
        right_column = str(hint.get("right_column") or "")
        if left_table and left_column and right_table and right_column:
            reason = str(hint.get("reason") or hint.get("description") or "").strip()
            suffix = f"；说明：{reason}" if reason else ""
            lines.append(f"-- {left_table}.{left_column} 可与 {right_table}.{right_column} 连接{suffix}")
    return "\n    ".join(lines) if lines else "-- 当前注入的表没有必须使用的跨表连接关系"


def build_result_filtering_prompt_payload(
    execute_context: Mapping[str, Any],
    *,
    question_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    rows = list(execute_context.get("rows", []))
    candidate_rows = [
        {
            "row_index": index,
            "values": json_ready(row),
        }
        for index, row in enumerate(rows)
    ]
    row_count = int(
        execute_context.get(
            "source_row_count",
            execute_context.get("row_count", len(rows)),
        )
    )
    candidate_row_count = len(candidate_rows)
    truncated = (
        bool(execute_context.get("truncated"))
        or bool(execute_context.get("row_limit_trimmed"))
        or row_count > candidate_row_count
        or len(rows) > candidate_row_count
    )
    question = dict(question_context or {})
    sql = execute_context.get("sql") or question.get("sql")
    return {
        "task_meta": {"stage": "result_filtering"},
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
            "format": "仅 JSON 对象",
            "required_fields": ["keep_row_indexes"],
            "optional_fields": ["filter_reason"],
            "keep_row_indexes": "来自 result_context.candidate_rows 的 0 起始 row_index 数组；不要返回完整行对象。",
        },
    }


def build_result_filtering_prompt(
    execute_context: Mapping[str, Any],
    *,
    question_context: Mapping[str, Any] | None = None,
) -> str:
    payload = build_result_filtering_prompt_payload(
        execute_context,
        question_context=question_context,
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
