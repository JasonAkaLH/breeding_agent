from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

from src.core.contracts import (
    CapabilityContract,
    CapabilityExecutionError,
    CapabilityExecutionRequest,
    CapabilityExecutionResult,
)
from src.core.models import Interrupt

from .helpers import find_dependency_output, make_artifact, make_audit_event, normalize_text
from .llm_utils import LLMOutputError, TextGenerator, call_text_generator, parse_json_object, string_list
from .prompt_builders import build_sql_generation_prompt

_APPROVAL_DETAIL_COLUMNS = (
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
)

_APPROVAL_LIST_COLUMNS = (
    "year",
    "approval_num",
    "crop_name",
    "variety_name",
    "applicant",
    "breeder",
    "suitable_area",
)


class SQLQuerySQLGenerateCapability(CapabilityContract):
    capability_id = "sql_query.sql_generate"
    version = "1"
    description = "Generate a readonly SQL candidate from SQLQuery route + schema context."

    def __init__(
        self,
        *,
        generator: Callable[[dict[str, Any]], str] | None = None,
        llm_text_generator: TextGenerator | None = None,
    ) -> None:
        self._fallback_generator = generator or self._generate_sql
        self._llm_text_generator = llm_text_generator

    async def execute(self, request: CapabilityExecutionRequest) -> CapabilityExecutionResult:
        context = find_dependency_output(request, ("selected_tables", "selected_columns", "user_question"))
        if self._llm_text_generator is None:
            return self._fallback_result(request, context, fallback_reason="llm_not_configured", llm_mode="not_configured")

        prompt = build_sql_generation_prompt(
            context,
            task_meta={"conversation_id": request.conversation_id, "task_id": request.task_id},
        )
        try:
            raw_output = await call_text_generator(self._llm_text_generator, prompt)
        except Exception as exc:
            return self._fallback_result(
                request,
                context,
                fallback_reason="provider_failed",
                llm_mode="provider_failed",
                diagnostic=str(exc),
            )

        try:
            llm_payload = parse_json_object(raw_output)
            mode = str(llm_payload.get("mode", "")).strip().lower()
            if mode == "answer":
                return self._answer_result(request, context, llm_payload)
            if mode == "clarify":
                return self._clarify_result(request, context, llm_payload)
            if mode == "reject":
                return self._reject_result(request, context, llm_payload)
            raise LLMOutputError("validation_failed", f"Unsupported LLM mode: {mode!r}")
        except LLMOutputError as exc:
            return self._fallback_result(
                request,
                context,
                fallback_reason=exc.reason,
                llm_mode=exc.reason,
                diagnostic=str(exc),
            )

    def _base_output(self, context: dict[str, Any]) -> dict[str, Any]:
        return {
            "route_id": context.get("route_id"),
            "schema_profile_id": context.get("schema_profile_id"),
            "allowed_tables": list(context.get("allowed_tables", [])),
            "selected_tables": list(context.get("selected_tables", [])),
            "selected_columns": dict(context.get("selected_columns", {})),
            "user_question": context.get("user_question"),
        }

    def _answer_result(
        self,
        request: CapabilityExecutionRequest,
        context: dict[str, Any],
        llm_payload: dict[str, Any],
    ) -> CapabilityExecutionResult:
        self._validate_route_context(context, llm_payload)
        sql = str(llm_payload.get("sql") or "").strip()
        if not sql:
            raise LLMOutputError("validation_failed", "LLM answer mode requires non-empty sql.")

        tables_used = string_list(llm_payload.get("tables_used"))
        allowed_tables = {str(table) for table in context.get("allowed_tables", [])}
        selected_tables = {str(table) for table in context.get("selected_tables", [])}
        allowed_scope = allowed_tables or selected_tables
        if not tables_used:
            raise LLMOutputError("validation_failed", "LLM answer mode requires tables_used.")
        if allowed_scope and not set(tables_used).issubset(allowed_scope):
            raise LLMOutputError("validation_failed", "LLM tables_used must be a subset of allowed tables.")

        columns_used = string_list(llm_payload.get("columns_used"))
        if not columns_used:
            raise LLMOutputError("validation_failed", "LLM answer mode requires columns_used.")
        self._validate_columns_used(context, columns_used)
        self._validate_sql_column_references(context, sql, tables_used=tables_used)
        self._validate_variety_name_matching_policy(sql)
        column_types_used = self._validate_column_types_used(context, llm_payload.get("column_types_used"), columns_used)

        output = {
            **self._base_output(context),
            "sql": sql,
            "tables_used": tables_used,
            "columns_used": columns_used,
            "column_types_used": column_types_used,
            "join_hints_used": string_list(llm_payload.get("join_hints_used")),
            "generation_source": "llm",
            "llm_mode": "answer",
            "fallback_used": False,
            "fallback_reason": None,
        }
        artifact = make_artifact(
            name="generated_sql",
            task_id=request.task_id,
            node_id=request.node_id,
            payload=output,
            summary=sql,
        )
        event = make_audit_event(
            request,
            event_type="sql_query.llm_call",
            payload={
                "node_name": self.capability_id,
                "status": "succeeded",
                "llm_mode": "answer",
                "fallback_used": False,
                "prompt_recorded": False,
            },
        )
        return CapabilityExecutionResult(
            capability_id=request.capability_id,
            task_id=request.task_id,
            node_id=request.node_id,
            output_payload=output,
            artifacts=(artifact,),
            events=(event,),
        )

    def _clarify_result(
        self,
        request: CapabilityExecutionRequest,
        context: dict[str, Any],
        llm_payload: dict[str, Any],
    ) -> CapabilityExecutionResult:
        self._validate_route_context(context, llm_payload)
        question = str(llm_payload.get("clarifying_question") or "").strip()
        if not question:
            raise LLMOutputError("validation_failed", "LLM clarify mode requires clarifying_question.")
        output = {
            **self._base_output(context),
            "llm_mode": "clarify",
            "generation_source": "llm",
            "fallback_used": False,
            "fallback_reason": None,
            "missing_info": llm_payload.get("missing_info"),
            "clarifying_question": question,
        }
        event = make_audit_event(
            request,
            event_type="sql_query.llm_call",
            payload={
                "node_name": self.capability_id,
                "status": "clarify",
                "llm_mode": "clarify",
                "fallback_used": False,
                "prompt_recorded": False,
            },
        )
        return CapabilityExecutionResult(
            capability_id=request.capability_id,
            task_id=request.task_id,
            node_id=request.node_id,
            output_payload=output,
            interrupt=Interrupt(
                interrupt_id=f"{request.node_id}:llm-clarify",
                conversation_id=request.conversation_id,
                task_id=request.task_id,
                node_id=request.node_id,
                source_agent=self.capability_id,
                source_message_id=f"{request.node_id}:llm-clarification",
                question=question,
                reason_code="llm_clarification_required",
                required_fields={"missing_info": llm_payload.get("missing_info")},
            ),
            events=(event,),
        )

    def _reject_result(
        self,
        request: CapabilityExecutionRequest,
        context: dict[str, Any],
        llm_payload: dict[str, Any],
    ) -> CapabilityExecutionResult:
        self._validate_route_context(context, llm_payload)
        reject_reason = str(llm_payload.get("reject_reason") or "").strip()
        supported_scope_hint = str(llm_payload.get("supported_scope_hint") or "").strip()
        if not reject_reason:
            raise LLMOutputError("validation_failed", "LLM reject mode requires reject_reason.")
        output = {
            **self._base_output(context),
            "llm_mode": "reject",
            "generation_source": "llm",
            "fallback_used": False,
            "fallback_reason": None,
            "reject_reason": reject_reason,
            "supported_scope_hint": supported_scope_hint or None,
        }
        event = make_audit_event(
            request,
            event_type="sql_query.llm_call",
            payload={
                "node_name": self.capability_id,
                "status": "rejected",
                "llm_mode": "reject",
                "fallback_used": False,
                "prompt_recorded": False,
            },
        )
        return CapabilityExecutionResult(
            capability_id=request.capability_id,
            task_id=request.task_id,
            node_id=request.node_id,
            output_payload=output,
            error=CapabilityExecutionError(
                code="llm_rejected_request",
                message=reject_reason,
                retriable=False,
                metadata={"supported_scope_hint": supported_scope_hint} if supported_scope_hint else {},
            ),
            events=(event,),
        )

    def _fallback_result(
        self,
        request: CapabilityExecutionRequest,
        context: dict[str, Any],
        *,
        fallback_reason: str,
        llm_mode: str,
        diagnostic: str | None = None,
    ) -> CapabilityExecutionResult:
        sql = self._fallback_generator(context)
        output = {
            **self._base_output(context),
            "sql": sql,
            "tables_used": list(context.get("selected_tables", [])),
            "columns_used": [
                f"{table}.{column}"
                for table, columns in dict(context.get("selected_columns", {})).items()
                for column in list(columns)
            ],
            "column_types_used": self._column_types_for_selected_columns(context),
            "join_hints_used": [],
            "generation_source": "fallback",
            "llm_mode": llm_mode,
            "fallback_used": True,
            "fallback_reason": fallback_reason,
        }
        artifact = make_artifact(
            name="generated_sql",
            task_id=request.task_id,
            node_id=request.node_id,
            payload=output,
            summary=sql,
        )
        event_payload = {
            "node_name": self.capability_id,
            "fallback_reason": fallback_reason,
            "llm_mode": llm_mode,
            "prompt_recorded": False,
        }
        if diagnostic:
            event_payload["diagnostic"] = diagnostic[:300]
        event = make_audit_event(request, event_type="sql_query.llm_fallback", payload=event_payload)
        return CapabilityExecutionResult(
            capability_id=request.capability_id,
            task_id=request.task_id,
            node_id=request.node_id,
            output_payload=output,
            artifacts=(artifact,),
            events=(event,),
        )


    def _allowed_columns_by_table(self, context: dict[str, Any]) -> dict[str, set[str]]:
        return {
            str(table): {str(column) for column in list(columns)}
            for table, columns in dict(context.get("selected_columns", {})).items()
        }

    def _column_types_for_selected_columns(self, context: dict[str, Any]) -> dict[str, str]:
        result: dict[str, str] = {}
        details = dict(context.get("selected_column_details", {}))
        for table, columns in details.items():
            for column in list(columns):
                if not isinstance(column, Mapping):
                    continue
                name = str(column.get("name") or "")
                sql_type = str(column.get("sql_type") or "")
                if name and sql_type:
                    result[f"{table}.{name}"] = sql_type
        return result

    def _validate_columns_used(self, context: dict[str, Any], columns_used: list[str]) -> None:
        allowed = self._allowed_columns_by_table(context)
        for raw_column in columns_used:
            table, column = self._resolve_column_reference(raw_column, allowed)
            if table is None or column is None:
                raise LLMOutputError("validation_failed", f"LLM column is not in selected schema: {raw_column}")

    def _validate_column_types_used(
        self,
        context: dict[str, Any],
        raw_column_types: Any,
        columns_used: list[str],
    ) -> dict[str, str]:
        expected_types = self._column_types_for_selected_columns(context)
        if not isinstance(raw_column_types, Mapping):
            raise LLMOutputError("validation_failed", "LLM answer mode requires column_types_used mapping.")

        normalized: dict[str, str] = {}
        allowed = self._allowed_columns_by_table(context)
        for raw_column in columns_used:
            table, column = self._resolve_column_reference(raw_column, allowed)
            if table is None or column is None:
                raise LLMOutputError("validation_failed", f"LLM column is not in selected schema: {raw_column}")
            canonical = f"{table}.{column}"
            actual_type = raw_column_types.get(canonical)
            if actual_type is None and raw_column in raw_column_types:
                actual_type = raw_column_types.get(raw_column)
            expected_type = expected_types.get(canonical)
            if not expected_type:
                raise LLMOutputError("validation_failed", f"No schema sql_type found for column: {canonical}")
            if str(actual_type) != expected_type:
                raise LLMOutputError("validation_failed", f"LLM sql_type does not match schema for column: {canonical}")
            normalized[canonical] = expected_type
        return normalized

    def _resolve_column_reference(self, raw_column: str, allowed: dict[str, set[str]]) -> tuple[str | None, str | None]:
        value = str(raw_column).strip().strip("`")
        if "." in value:
            table, column = value.split(".", 1)
            table = table.strip("`")
            column = column.strip("`")
            if column in allowed.get(table, set()):
                return table, column
            return None, None

        matches = [(table, value) for table, columns in allowed.items() if value in columns]
        if len(matches) == 1:
            return matches[0]
        return None, None

    def _validate_sql_column_references(self, context: dict[str, Any], sql: str, *, tables_used: list[str]) -> None:
        allowed_all = self._allowed_columns_by_table(context)
        used_scope = {str(table) for table in tables_used}
        allowed = {table: columns for table, columns in allowed_all.items() if not used_scope or table in used_scope}
        table_aliases = self._extract_table_aliases(sql)
        for qualifier, column in re.findall(r"\b`?([A-Za-z_][\w]*)`?\.`?([A-Za-z_][\w]*)`?\b", sql):
            table = table_aliases.get(qualifier, qualifier)
            if table not in allowed or column not in allowed.get(table, set()):
                raise LLMOutputError("validation_failed", f"SQL references column outside selected schema: {qualifier}.{column}")

        for column in self._extract_unqualified_column_references(sql, table_aliases):
            if self._resolve_column_reference(column, allowed) == (None, None):
                raise LLMOutputError("validation_failed", f"SQL references column outside selected schema: {column}")

    def _extract_table_aliases(self, sql: str) -> dict[str, str]:
        aliases: dict[str, str] = {}
        for table, alias in re.findall(
            r"\b(?:from|join)\s+`?([A-Za-z_][\w]*)`?(?:\s+(?:as\s+)?`?([A-Za-z_][\w]*)`?)?",
            sql,
            flags=re.I,
        ):
            aliases[table] = table
            if alias and alias.lower() not in {"on", "where", "join", "left", "right", "inner", "limit", "group", "order"}:
                aliases[alias] = table
        return aliases

    def _extract_unqualified_column_references(self, sql: str, table_aliases: dict[str, str]) -> set[str]:
        without_strings = re.sub(r"'[^']*'|\"[^\"]*\"", " ", sql)
        without_qualified = re.sub(r"\b`?[A-Za-z_][\w]*`?\.`?[A-Za-z_][\w]*`?\b", " ", without_strings)
        tokens = set(re.findall(r"\b[A-Za-z_][\w]*\b", without_qualified))
        keywords = {
            "select", "from", "join", "left", "right", "inner", "outer", "on", "where", "and", "or",
            "limit", "order", "group", "by", "as", "like", "in", "is", "null", "not", "with", "distinct",
            "count", "sum", "avg", "min", "max", "case", "when", "then", "else", "end", "desc", "asc",
        }
        return {
            token.strip("`")
            for token in tokens
            if token.lower() not in keywords and token not in table_aliases
        }

    def _validate_route_context(self, context: dict[str, Any], llm_payload: dict[str, Any]) -> None:
        expected_route = context.get("route_id")
        expected_profile = context.get("schema_profile_id")
        actual_route = llm_payload.get("route_id")
        actual_profile = llm_payload.get("schema_profile_id")
        if actual_route is not None and actual_route != expected_route:
            raise LLMOutputError("validation_failed", "LLM route_id does not match upstream context.")
        if actual_profile is not None and actual_profile != expected_profile:
            raise LLMOutputError("validation_failed", "LLM schema_profile_id does not match upstream context.")

    def _generate_sql(self, context: dict[str, Any]) -> str:
        selected_tables = list(context.get("selected_tables", []))
        selected_columns = dict(context.get("selected_columns", {}))
        join_hints = list(context.get("join_hints", []))
        user_question = normalize_text(context.get("user_question", ""))

        if context.get("route_id") == "variety_overview":
            return self._generate_variety_overview_sql(context)
        if context.get("route_id") == "approval_variety_db":
            return self._generate_approval_variety_sql(context)

        base_table = selected_tables[0]
        projected_columns: list[str] = []
        for table in selected_tables[:2]:
            for column in selected_columns.get(table, [])[:2]:
                if len(selected_tables) > 1:
                    projected_columns.append(f"{table}.{column}")
                else:
                    projected_columns.append(column)
        if not projected_columns:
            projected_columns = ["*"]

        if any(keyword in user_question for keyword in ("多少", "数量", "count", "几条")):
            projected_columns = ["COUNT(*) AS total"]

        sql = f"SELECT {', '.join(projected_columns)} FROM {base_table}"
        joined_tables = {base_table}
        for hint in join_hints:
            right_table = hint["right_table"]
            if right_table in joined_tables:
                continue
            if right_table not in selected_tables:
                continue
            sql += (
                f" JOIN {right_table} ON "
                f"{hint['left_table']}.{hint['left_column']} = {hint['right_table']}.{hint['right_column']}"
            )
            joined_tables.add(right_table)

        term = self._extract_variety_search_term(user_question)
        safe_term = self._safe_search_literal(term) if term else None
        where_columns = tuple(
            f"{table}.variety_name"
            for table in joined_tables
            if "variety_name" in {str(column) for column in selected_columns.get(table, [])}
        )
        sql += self._where_search_term(safe_term, where_columns)
        sql += " LIMIT 50"
        return sql

    def _generate_approval_variety_sql(self, context: dict[str, Any]) -> str:
        selected_tables = [str(table) for table in context.get("selected_tables", []) if str(table).endswith("_varieties")]
        selected_columns = dict(context.get("selected_columns", {}))
        user_question = str(context.get("user_question") or "")
        normalized_question = normalize_text(user_question)
        if not selected_tables:
            return "SELECT COUNT(*) AS total FROM rice_varieties LIMIT 50"

        base_table = selected_tables[0]
        if any(keyword in normalized_question for keyword in ("多少", "数量", "count", "几条")):
            return f"SELECT COUNT(*) AS total FROM {base_table} LIMIT 50"

        detail_requested = any(
            keyword in normalized_question
            for keyword in ("详细", "详情", "所有信息", "全部信息", "完整", "具体", "介绍", "信息")
        )
        preferred_columns = _APPROVAL_DETAIL_COLUMNS if detail_requested else _APPROVAL_LIST_COLUMNS
        available_columns = {str(column) for column in selected_columns.get(base_table, [])}
        projected = [column for column in preferred_columns if column in available_columns]
        if not projected:
            projected = [str(column) for column in selected_columns.get(base_table, [])[:8]]
        if not projected:
            projected = ["*"]

        term = self._extract_variety_search_term(user_question)
        safe_term = self._safe_search_literal(term) if term else None
        where = self._where_search_term(safe_term, (f"{base_table}.variety_name",))
        projection = ", ".join(f"{base_table}.{column}" for column in projected) if projected != ["*"] else "*"
        return f"SELECT {projection} FROM {base_table}{where} LIMIT 50"

    def _generate_variety_overview_sql(self, context: dict[str, Any]) -> str:
        allowed_tables = {str(table) for table in context.get("allowed_tables", [])}
        selected_tables = {str(table) for table in context.get("selected_tables", [])}
        table_scope = allowed_tables or selected_tables
        term = self._extract_variety_search_term(str(context.get("user_question") or ""))
        safe_term = self._safe_search_literal(term) if term else None

        approval_tables = self._overview_approval_tables(
            table_scope,
            user_question=str(context.get("user_question") or ""),
            term=term,
        )
        selects: list[str] = []
        for table in approval_tables:
            if table == "rice_varieties" and {"variety", "rice_comp"}.issubset(table_scope):
                where = self._where_search_term(
                    safe_term,
                    ("rice_varieties.variety_name",),
                )
                selects.append(
                    "SELECT 'approval_variety_db' AS source_library, "
                    "rice_varieties.crop_name, rice_varieties.variety_name, rice_varieties.approval_num, "
                    "rice_varieties.year, rice_varieties.applicant, rice_varieties.breeder, "
                    "variety.variety_id AS genotype_variety_id, "
                    "rice_comp.all_indica_comp, rice_comp.all_japonica_comp, rice_comp.indica_japonica_mix_comp "
                    "FROM rice_varieties "
                    "LEFT JOIN variety ON rice_varieties.ref_var_id = variety.variety_id "
                    "LEFT JOIN rice_comp ON variety.variety_id = rice_comp.variety_id"
                    f"{where}"
                )
                continue

            where = self._where_search_term(safe_term, (f"{table}.variety_name",))
            selects.append(
                "SELECT 'approval_variety_db' AS source_library, "
                f"{table}.crop_name, {table}.variety_name, {table}.approval_num, "
                f"{table}.year, {table}.applicant, {table}.breeder, "
                "NULL AS genotype_variety_id, "
                "NULL AS all_indica_comp, NULL AS all_japonica_comp, NULL AS indica_japonica_mix_comp "
                f"FROM {table}"
                f"{where}"
            )

        if "variety" in table_scope:
            has_rice_comp = "rice_comp" in table_scope
            where_columns = ["variety.variety_name"]
            where = self._where_search_term(safe_term, tuple(where_columns))
            rice_comp_projection = (
                "rice_comp.all_indica_comp, rice_comp.all_japonica_comp, rice_comp.indica_japonica_mix_comp"
                if has_rice_comp
                else "NULL AS all_indica_comp, NULL AS all_japonica_comp, NULL AS indica_japonica_mix_comp"
            )
            rice_comp_join = " LEFT JOIN rice_comp ON variety.variety_id = rice_comp.variety_id" if has_rice_comp else ""
            selects.append(
                "SELECT 'genotype_db' AS source_library, "
                "NULL AS crop_name, variety.variety_name, NULL AS approval_num, "
                "NULL AS year, NULL AS applicant, NULL AS breeder, "
                "variety.variety_id AS genotype_variety_id, "
                f"{rice_comp_projection} "
                "FROM variety"
                f"{rice_comp_join}"
                f"{where}"
            )

        if not selects:
            return "SELECT COUNT(*) AS total FROM variety LIMIT 50"
        return " UNION ALL ".join(selects) + " LIMIT 50"

    def _extract_variety_search_term(self, user_question: str) -> str | None:
        normalized = str(user_question or "").replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
        cleaned = normalized
        for keyword in ("请", "帮我", "给我", "再", "查询", "查一下", "查查", "看一下", "看看", "了解一下", "品种", "信息", "的"):
            cleaned = cleaned.replace(keyword, " ")

        numbered_match = re.search(r"[\u4e00-\u9fffA-Za-z]{1,16}\d+[A-Za-z0-9\u4e00-\u9fff_-]*", cleaned)
        if numbered_match:
            term = self._normalize_variety_search_term(numbered_match.group(0))
            if self._is_meaningful_variety_search_term(term):
                return term

        explicit_match = re.search(
            r"(?:品种名称|品种名|名字|名称)[^，。；,;?？]{0,10}(?:带|包含|含有|包括)['\"]?([^'\"，。；,;?？\s]{1,24})",
            normalized,
        )
        if explicit_match:
            term = self._normalize_variety_search_term(explicit_match.group(1))
            if self._is_meaningful_variety_search_term(term):
                return term

        series_text = normalized
        for keyword in (
            "请",
            "帮我",
            "给我",
            "再",
            "查询",
            "查一下",
            "查查",
            "看一下",
            "看看",
            "了解一下",
            "都有什么",
            "有什么",
            "有哪些",
            "什么",
            "品种",
            "信息",
            "的",
        ):
            series_text = series_text.replace(keyword, " ")
        series_match = re.search(r"([^\s，。；,;?？'\"、]{1,16})系列", series_text)
        if series_match:
            term = self._normalize_variety_search_term(series_match.group(1))
            if self._is_meaningful_variety_search_term(term):
                return term

        return None

    def _normalize_variety_search_term(self, term: str) -> str:
        value = str(term or "").strip().strip(" \"'`，。；,;?？、")
        value = re.split(r"(?:系列|的|是|为|都|有|包括|包含|含有)", value, maxsplit=1)[0]
        for phrase in ("请", "帮我", "给我", "再", "查询", "查一下", "查查", "看一下", "看看", "了解一下"):
            value = value.replace(phrase, "")
        return value.strip().strip(" \"'`，。；,;?？、")

    def _is_meaningful_variety_search_term(self, term: str) -> bool:
        if not term:
            return False
        if term in {"系列", "品种", "信息", "名字", "名称", "什么", "哪些"}:
            return False
        return bool(re.search(r"[\u4e00-\u9fffA-Za-z0-9]", term))

    def _safe_search_literal(self, term: str) -> str:
        safe = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9_-]", "", term)
        return safe[:80]

    def _overview_approval_tables(self, table_scope: set[str], *, user_question: str, term: str | None) -> list[str]:
        text = f"{user_question} {term or ''}"
        crop_specific_tables = (
            ("rice_varieties", ("水稻", "粳", "籼", "稻")),
            ("corn_varieties", ("玉米",)),
            ("cotton_varieties", ("棉花", "棉")),
            ("wheat_varieties", ("小麦", "麦")),
            ("soybean_varieties", ("大豆", "豆")),
        )
        for table_name, hints in crop_specific_tables:
            if table_name in table_scope and any(hint in text for hint in hints):
                return [table_name]
        return [
            table
            for table in ("corn_varieties", "rice_varieties", "cotton_varieties", "wheat_varieties", "soybean_varieties")
            if table in table_scope
        ]

    def _where_search_term(self, safe_term: str | None, columns: tuple[str, ...]) -> str:
        if not safe_term:
            return ""
        clauses = [f"{column} LIKE '%{safe_term}%'" for column in columns]
        return " WHERE " + " OR ".join(clauses)

    def _validate_variety_name_matching_policy(self, sql: str) -> None:
        left_strict_equal = r"(?:`?[A-Za-z_][\w]*`?\.)?`?variety_name`?\s*=\s*(?:'[^']*'|\"[^\"]*\"|`?[A-Za-z_][\w]*`?(?:\.`?[A-Za-z_][\w]*`?)?)"
        right_strict_equal = r"(?:'[^']*'|\"[^\"]*\")\s*=\s*(?:`?[A-Za-z_][\w]*`?\.)?`?variety_name`?"
        if re.search(left_strict_equal, sql, flags=re.I) or re.search(right_strict_equal, sql, flags=re.I):
            raise LLMOutputError(
                "validation_failed",
                "SQL must use LIKE instead of strict equality when filtering variety_name.",
            )
