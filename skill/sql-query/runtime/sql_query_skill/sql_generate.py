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

from .helpers import (
    SQL_QUERY_AUDIT_LLM_CALL_EVENT,
    SQL_QUERY_AUDIT_LLM_FALLBACK_EVENT,
    SQL_QUERY_PUBLIC_CAPABILITY_ID,
    find_dependency_output,
    make_artifact,
    make_audit_event,
    normalize_text,
    sql_fingerprint,
)
from .llm_utils import LLMOutputError, TextGenerator, call_text_generator, parse_json_object, string_list
from .prompt_builders import build_sql_generation_prompt, build_sql_repair_prompt
from .sql_ast import (
    SQLAstAnalysis,
    SQLAstBranch,
    SQLAstError,
    analyze_sql,
    branch_has_constraint,
    branch_has_count,
    branch_projection_literal,
    final_has_limit,
    final_has_order,
)

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

_TABLE_ALIAS_PATTERN = (
    r"\b(?:from|join)\s+`?([A-Za-z_][\w]*)`?"
    r"(?:\s+(?:as\s+)?`?(?!on\b|where\b|join\b|left\b|right\b|inner\b|outer\b|limit\b|group\b|order\b)"
    r"([A-Za-z_][\w]*)`?)?"
)

class SQLQuerySQLGenerateCapability(CapabilityContract):
    capability_id = SQL_QUERY_PUBLIC_CAPABILITY_ID
    version = "1"
    description = "根据 SQLQuery 路由和 schema 上下文生成只读 SQL 候选。"

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
        if not list(context.get("selected_tables") or []):
            return CapabilityExecutionResult(
                capability_id=request.capability_id,
                task_id=request.task_id,
                node_id=request.node_id,
                error=CapabilityExecutionError(
                    code="selected_tables_required",
                    message="SQL generation requires schema_resolution selected_tables.",
                    retriable=False,
                    metadata={
                        "failed_stage": "sql_generate",
                        "table_scope_authority": "schema_resolution",
                    },
                ),
            )
        repair_context = _repair_context_from_request(request)
        clarification = _query_constraint_clarification(context)
        if clarification is not None:
            return self._constraint_clarify_result(request, context, clarification)
        if self._llm_text_generator is None:
            return self._fallback_result(
                request,
                context,
                fallback_reason="sql_repair_llm_not_configured" if repair_context else "llm_not_configured",
                llm_mode="repair_not_configured" if repair_context else "not_configured",
                repair_context=repair_context,
            )

        task_meta = {"conversation_id": request.conversation_id, "task_id": request.task_id}
        if repair_context:
            prompt = build_sql_repair_prompt(context, repair_context=repair_context, task_meta=task_meta)
        else:
            prompt = build_sql_generation_prompt(
                context,
                task_meta=task_meta,
            )
        try:
            raw_output = await call_text_generator(self._llm_text_generator, prompt, request=request)
        except Exception as exc:
            return self._fallback_result(
                request,
                context,
                fallback_reason="sql_repair_provider_failed" if repair_context else "provider_failed",
                llm_mode="repair_provider_failed" if repair_context else "provider_failed",
                diagnostic=str(exc),
                repair_context=repair_context,
            )

        llm_payload: dict[str, Any] | None = None
        try:
            try:
                llm_payload = parse_json_object(raw_output)
            except LLMOutputError:
                llm_payload = self._raw_sql_payload_from_output(raw_output, context)
            mode = str(llm_payload.get("mode", "")).strip().lower()
            if mode == "answer":
                return self._answer_result(request, context, llm_payload, repair_context=repair_context)
            if mode == "clarify":
                if repair_context:
                    return self._repair_generation_failed_result(
                        request,
                        context,
                        repair_context=repair_context,
                        reason="repair_returned_clarify",
                    )
                return self._clarify_result(request, context, llm_payload)
            if mode == "reject":
                if repair_context:
                    return self._repair_generation_failed_result(
                        request,
                        context,
                        repair_context=repair_context,
                        reason="repair_returned_reject",
                    )
                return self._reject_result(request, context, llm_payload)
            raise LLMOutputError("validation_failed", f"Unsupported LLM mode: {mode!r}")
        except LLMOutputError as exc:
            if (
                repair_context is None
                and self._should_defer_validation_to_engine(request, exc)
            ):
                return self._local_validation_failed_result(
                    request,
                    context,
                    reason=exc.reason,
                    diagnostic=str(exc),
                    raw_output=raw_output,
                    llm_payload=llm_payload,
                )
            return self._fallback_result(
                request,
                context,
                fallback_reason=exc.reason,
                llm_mode=f"repair_{exc.reason}" if repair_context else exc.reason,
                diagnostic=str(exc),
                repair_context=repair_context,
            )

    def _base_output(self, context: dict[str, Any]) -> dict[str, Any]:
        return {
            "route_id": context.get("route_id"),
            "schema_profile_id": context.get("schema_profile_id"),
            "allowed_tables": list(context.get("allowed_tables", [])),
            "selected_tables": list(context.get("selected_tables", [])),
            "selected_columns": dict(context.get("selected_columns", {})),
            "user_question": context.get("user_question"),
            "original_user_query": context.get("original_user_query") or context.get("user_question"),
            "resolved_user_query": context.get("resolved_user_query") or context.get("user_question"),
            "parent_question": context.get("parent_question"),
            "subtask_label": context.get("subtask_label"),
            "schema_ddl": context.get("schema_ddl") or context.get("database_schema"),
            "entities": list(context.get("entities", [])),
            "probe_summary": context.get("probe_summary"),
            "match_summary": context.get("match_summary"),
            "matched_fields": list(context.get("matched_fields", [])),
            "match_tiers": list(context.get("match_tiers", [])),
            "search_effort_summary": context.get("search_effort_summary"),
            "query_constraints": context.get("query_constraints"),
            "constraint_coverage_summary": context.get("constraint_coverage_summary"),
        }

    def _answer_result(
        self,
        request: CapabilityExecutionRequest,
        context: dict[str, Any],
        llm_payload: dict[str, Any],
        *,
        repair_context: Mapping[str, Any] | None = None,
    ) -> CapabilityExecutionResult:
        self._validate_route_context(context, llm_payload)
        sql = str(llm_payload.get("sql") or "").strip()
        if not sql:
            raise LLMOutputError("validation_failed", "LLM answer mode requires non-empty sql.")
        ast_analysis = _analyze_generated_sql(sql) if ";" not in sql else None

        tables_used = string_list(llm_payload.get("tables_used"))
        selected_tables = {str(table) for table in context.get("selected_tables", [])}
        if not tables_used:
            raise LLMOutputError("validation_failed", "LLM answer mode requires tables_used.")
        if not selected_tables:
            raise LLMOutputError("validation_failed", "SQL generation requires schema_resolution selected_tables.")
        if not set(tables_used).issubset(selected_tables):
            raise LLMOutputError("validation_failed", "LLM tables_used must be a subset of selected_tables.")

        columns_used = string_list(llm_payload.get("columns_used"))
        if not columns_used:
            raise LLMOutputError("validation_failed", "LLM answer mode requires columns_used.")
        self._validate_columns_used(context, columns_used)
        self._validate_sql_column_references(context, sql, tables_used=tables_used, ast_analysis=ast_analysis)
        self._validate_variety_name_matching_policy(sql)
        self._validate_source_projection_policy(context, sql, tables_used=tables_used)
        self._validate_entity_filter_policy(context, sql, ast_analysis=ast_analysis)
        constraint_coverage_summary = self._validate_constraint_coverage(context, sql, tables_used=tables_used, ast_analysis=ast_analysis)
        column_types_used = self._validate_column_types_used(context, llm_payload.get("column_types_used"), columns_used)

        output = {
            **self._base_output(context),
            "sql": sql,
            "tables_used": tables_used,
            "source_scope": _source_scope_from_tables(tables_used, context=context),
            "columns_used": columns_used,
            "column_types_used": column_types_used,
            "join_hints_used": string_list(llm_payload.get("join_hints_used")),
            "generation_source": "llm",
            "llm_mode": "repair_answer" if repair_context else "answer",
            "llm_output_format": str(llm_payload.get("_output_format") or "json"),
            "fallback_used": False,
            "fallback_reason": None,
            "constraint_coverage_summary": constraint_coverage_summary,
        }
        if repair_context:
            output["sql_repair_attempt"] = int(repair_context.get("attempt") or 1)
            output["sql_repair_from"] = {
                "failed_stage": repair_context.get("failed_stage"),
                "error_code": repair_context.get("error_code"),
                "sql_fingerprint": repair_context.get("sql_fingerprint"),
            }
        artifact = make_artifact(
            name=f"generated_sql_repair{repair_context.get('attempt')}" if repair_context else "generated_sql",
            task_id=request.task_id,
            node_id=request.node_id,
            payload=output,
            summary=sql,
        )
        event = make_audit_event(
            request,
            event_type=SQL_QUERY_AUDIT_LLM_CALL_EVENT,
            payload={
                "capability_id": self.capability_id, "stage": "sql_generate",
                "status": "succeeded",
                "llm_mode": "repair_answer" if repair_context else "answer",
                "fallback_used": False,
                "prompt_recorded": False,
                "repair_attempt": repair_context.get("attempt") if repair_context else None,
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

    def _local_validation_failed_result(
        self,
        request: CapabilityExecutionRequest,
        context: dict[str, Any],
        *,
        reason: str,
        diagnostic: str,
        raw_output: str,
        llm_payload: Mapping[str, Any] | None,
    ) -> CapabilityExecutionResult:
        failed_sql = self._best_effort_failed_sql(raw_output, llm_payload)
        output = {
            **self._base_output(context),
            "sql": failed_sql,
            "generation_source": "llm",
            "llm_mode": reason,
            "fallback_used": False,
            "fallback_reason": None,
            "validation_error": reason,
        }
        event = make_audit_event(
            request,
            event_type=SQL_QUERY_AUDIT_LLM_CALL_EVENT,
            payload={
                "capability_id": self.capability_id,
                "stage": "sql_generate",
                "status": "validation_failed",
                "llm_mode": reason,
                "fallback_used": False,
                "prompt_recorded": False,
                "diagnostic": diagnostic[:300],
            },
        )
        return CapabilityExecutionResult(
            capability_id=request.capability_id,
            task_id=request.task_id,
            node_id=request.node_id,
            output_payload=output,
            error=CapabilityExecutionError(
                code="sql_generation_validation_failed",
                message="Generated SQL did not pass local validation.",
                retriable=False,
                metadata={
                    "failed_stage": "sql_generate",
                    "repairable_sql_error": True,
                    "validation_reason": reason,
                    "sql_fingerprint": sql_fingerprint(failed_sql),
                },
            ),
            events=(event,),
        )

    def _repair_generation_failed_result(
        self,
        request: CapabilityExecutionRequest,
        context: dict[str, Any],
        *,
        repair_context: Mapping[str, Any],
        reason: str,
    ) -> CapabilityExecutionResult:
        output = {
            **self._base_output(context),
            "generation_source": "llm",
            "llm_mode": "repair_failed",
            "fallback_used": False,
            "fallback_reason": None,
            "sql_repair_attempt": int(repair_context.get("attempt") or 1),
            "sql_repair_from": {
                "failed_stage": repair_context.get("failed_stage"),
                "error_code": repair_context.get("error_code"),
                "sql_fingerprint": repair_context.get("sql_fingerprint"),
            },
        }
        event = make_audit_event(
            request,
            event_type=SQL_QUERY_AUDIT_LLM_CALL_EVENT,
            payload={
                "capability_id": self.capability_id,
                "stage": "sql_generate",
                "status": "repair_failed",
                "llm_mode": "repair_failed",
                "repair_attempt": repair_context.get("attempt"),
                "reason": reason,
                "prompt_recorded": False,
            },
        )
        return CapabilityExecutionResult(
            capability_id=request.capability_id,
            task_id=request.task_id,
            node_id=request.node_id,
            output_payload=output,
            error=CapabilityExecutionError(
                code="sql_repair_generation_failed",
                message=f"SQL repair generation failed: {reason}",
                retriable=False,
                metadata={
                    "repairable_sql_error": False,
                    "repair_attempt": int(repair_context.get("attempt") or 1),
                    "failed_stage": repair_context.get("failed_stage"),
                    "last_error_code": repair_context.get("error_code"),
                },
            ),
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
            "ok": False,
            "status": "missing_input",
            "needs_user_input": True,
            "answer": question,
            "response_text": question,
            "error": {"type": "missing_input", "message": question},
            "presentation": "natural_language",
            "missing_info": llm_payload.get("missing_info"),
            "clarifying_question": question,
        }
        event = make_audit_event(
            request,
            event_type=SQL_QUERY_AUDIT_LLM_CALL_EVENT,
            payload={
                "capability_id": self.capability_id, "stage": "sql_generate",
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
                required_fields={
                    "missing_info": llm_payload.get("missing_info"),
                    "_sql_query_resolution": {
                        "domain_kind": "sql_query",
                        "presentation": "natural_language",
                        "reason_code": "llm_clarification_required",
                    },
                },
            ),
            events=(event,),
        )

    def _constraint_clarify_result(
        self,
        request: CapabilityExecutionRequest,
        context: dict[str, Any],
        clarification: Mapping[str, Any],
    ) -> CapabilityExecutionResult:
        question = str(clarification.get("question") or "").strip() or "请补充查询条件后再试。"
        missing = [str(item) for item in list(clarification.get("missing") or []) if str(item).strip()]
        output = {
            **self._base_output(context),
            "llm_mode": "constraint_clarify",
            "generation_source": "deterministic",
            "fallback_used": False,
            "fallback_reason": None,
            "ok": False,
            "status": "missing_input",
            "needs_user_input": True,
            "answer": question,
            "response_text": question,
            "error": {"type": "missing_input", "message": question},
            "missing": missing,
            "presentation": "natural_language",
            "clarifying_question": question,
            "clarification_reason": clarification.get("reason"),
        }
        event = make_audit_event(
            request,
            event_type=SQL_QUERY_AUDIT_LLM_CALL_EVENT,
            payload={
                "capability_id": self.capability_id,
                "stage": "sql_generate",
                "status": "constraint_clarify",
                "llm_mode": "constraint_clarify",
                "fallback_used": False,
                "prompt_recorded": False,
                "reason": clarification.get("reason"),
            },
        )
        required_fields = {name: {} for name in missing}
        required_fields["_sql_query_resolution"] = {
            "domain_kind": "sql_query",
            "presentation": "natural_language",
            "reason_code": "query_constraint_clarification_required",
            "clarification_reason": clarification.get("reason"),
        }
        return CapabilityExecutionResult(
            capability_id=request.capability_id,
            task_id=request.task_id,
            node_id=request.node_id,
            output_payload=output,
            interrupt=Interrupt(
                interrupt_id=f"{request.node_id}:constraint-clarify",
                conversation_id=request.conversation_id,
                task_id=request.task_id,
                node_id=request.node_id,
                source_agent=self.capability_id,
                source_message_id=f"{request.node_id}:constraint-clarification",
                question=question,
                reason_code="query_constraint_clarification_required",
                required_fields=required_fields,
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
            event_type=SQL_QUERY_AUDIT_LLM_CALL_EVENT,
            payload={
                "capability_id": self.capability_id, "stage": "sql_generate",
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
        repair_context: Mapping[str, Any] | None = None,
    ) -> CapabilityExecutionResult:
        sql = self._fallback_generator(context)
        fallback_validation_error: CapabilityExecutionError | None = None
        constraint_coverage_summary: dict[str, Any] | None = None
        try:
            ast_analysis = _analyze_generated_sql(sql) if ";" not in sql else None
            tables_used = self._infer_tables_used_from_sql(context, sql, ast_analysis=ast_analysis)
            columns_used = self._infer_columns_used_from_sql(context, sql, tables_used=tables_used, ast_analysis=ast_analysis)
            self._validate_columns_used(context, columns_used)
            self._validate_sql_column_references(context, sql, tables_used=tables_used, ast_analysis=ast_analysis)
            self._validate_variety_name_matching_policy(sql)
            self._validate_source_projection_policy(context, sql, tables_used=tables_used)
            self._validate_entity_filter_policy(context, sql, ast_analysis=ast_analysis)
            constraint_coverage_summary = self._validate_constraint_coverage(context, sql, tables_used=tables_used, ast_analysis=ast_analysis)
            column_types_used = self._validate_column_types_used(context, None, columns_used)
        except LLMOutputError as exc:
            tables_used = list(context.get("selected_tables", []))
            columns_used = [
                f"{table}.{column}"
                for table, columns in dict(context.get("selected_columns", {})).items()
                for column in list(columns)
            ]
            column_types_used = self._column_types_for_selected_columns(context)
            if re.match(r"^\s*(select|with)\b", sql, flags=re.I):
                fallback_validation_error = CapabilityExecutionError(
                    code="sql_fallback_validation_failed",
                    message="Fallback SQL did not pass local validation.",
                    retriable=False,
                    metadata={
                        "failed_stage": "sql_generate",
                        "repairable_sql_error": False,
                        "validation_reason": exc.reason,
                        "sql_fingerprint": sql_fingerprint(sql),
                    },
                )
        output = {
            **self._base_output(context),
            "sql": sql,
            "tables_used": tables_used,
            "source_scope": _source_scope_from_tables(tables_used, context=context),
            "columns_used": columns_used,
            "column_types_used": column_types_used,
            "join_hints_used": [],
            "generation_source": "fallback",
            "llm_mode": llm_mode,
            "fallback_used": True,
            "fallback_reason": fallback_reason,
            "constraint_coverage_summary": constraint_coverage_summary,
        }
        if repair_context:
            output["sql_repair_attempt"] = int(repair_context.get("attempt") or 1)
            output["sql_repair_from"] = {
                "failed_stage": repair_context.get("failed_stage"),
                "error_code": repair_context.get("error_code"),
                "sql_fingerprint": repair_context.get("sql_fingerprint"),
            }
        artifact = make_artifact(
            name=f"generated_sql_repair{repair_context.get('attempt')}" if repair_context else "generated_sql",
            task_id=request.task_id,
            node_id=request.node_id,
            payload=output,
            summary=sql,
        )
        event_payload = {
            "capability_id": self.capability_id, "stage": "sql_generate",
            "fallback_reason": fallback_reason,
            "llm_mode": llm_mode,
            "prompt_recorded": False,
        }
        if repair_context:
            event_payload["repair_attempt"] = repair_context.get("attempt")
        if diagnostic:
            event_payload["diagnostic"] = diagnostic[:300]
        event = make_audit_event(request, event_type=SQL_QUERY_AUDIT_LLM_FALLBACK_EVENT, payload=event_payload)
        return CapabilityExecutionResult(
            capability_id=request.capability_id,
            task_id=request.task_id,
            node_id=request.node_id,
            output_payload=output,
            artifacts=(artifact,),
            events=(event,),
            error=fallback_validation_error,
        )


    def _raw_sql_payload_from_output(self, raw_output: str, context: dict[str, Any]) -> dict[str, Any]:
        sql = self._extract_sql_from_text(raw_output)
        ast_analysis = _analyze_generated_sql(sql) if ";" not in sql else None
        tables_used = self._infer_tables_used_from_sql(context, sql, ast_analysis=ast_analysis)
        columns_used = self._infer_columns_used_from_sql(context, sql, tables_used=tables_used, ast_analysis=ast_analysis)
        return {
            "mode": "answer",
            "route_id": context.get("route_id"),
            "schema_profile_id": context.get("schema_profile_id"),
            "sql": sql,
            "tables_used": tables_used,
            "columns_used": columns_used,
            "join_hints_used": [],
            "_output_format": "raw_sql",
        }

    def _extract_sql_from_text(self, raw_output: str) -> str:
        value = str(raw_output or "").strip()
        if not value:
            raise LLMOutputError("parse_failed", "LLM raw SQL output is empty.")

        fenced = re.search(r"```(?:sql)?\s*([\s\S]*?)```", value, flags=re.I)
        if fenced:
            value = fenced.group(1).strip()
        else:
            open_fence = re.search(r"```(?:sql)?\s*([\s\S]*)", value, flags=re.I)
            if open_fence:
                value = open_fence.group(1).strip()

        value = value.strip().strip("`").strip()
        if value.endswith(";"):
            value = value[:-1].strip()
        if not re.match(r"^(select|with)\b", value, flags=re.I):
            raise LLMOutputError("parse_failed", "LLM output is neither JSON nor a raw SELECT SQL statement.")
        return value

    def _infer_tables_used_from_sql(self, context: dict[str, Any], sql: str, *, ast_analysis: SQLAstAnalysis | None = None) -> list[str]:
        selected_tables = {str(table) for table in context.get("selected_tables", [])}
        if not selected_tables:
            raise LLMOutputError("validation_failed", "Raw SQL validation requires schema_resolution selected_tables.")
        tables_used: list[str] = []
        ast_tables = list(ast_analysis.tables) if ast_analysis is not None else []
        raw_tables = ast_tables or [table for table, _alias in re.findall(_TABLE_ALIAS_PATTERN, sql, flags=re.I)]
        for table in raw_tables:
            normalized_table = str(table)
            if normalized_table not in selected_tables:
                raise LLMOutputError("validation_failed", f"Raw SQL references table outside selected_tables: {normalized_table}")
            if normalized_table not in tables_used:
                tables_used.append(normalized_table)
        if not tables_used:
            raise LLMOutputError("validation_failed", "Raw SQL output does not reference an allowed table.")
        return tables_used

    def _infer_columns_used_from_sql(
        self,
        context: dict[str, Any],
        sql: str,
        *,
        tables_used: list[str],
        ast_analysis: SQLAstAnalysis | None = None,
    ) -> list[str]:
        allowed = self._allowed_columns_by_table(context)
        table_aliases = self._extract_table_aliases(sql)
        columns_used: list[str] = []

        def append(table: str, column: str) -> None:
            canonical = f"{table}.{column}"
            if table in allowed and column in allowed[table] and canonical not in columns_used:
                columns_used.append(canonical)

        used_scope = {str(table) for table in tables_used}
        scoped_allowed = {table: columns for table, columns in allowed.items() if table in used_scope}
        if ast_analysis is not None:
            for branch in ast_analysis.branches:
                branch_scope = {table for table in branch.tables if table in used_scope} or used_scope
                branch_allowed = {table: columns for table, columns in allowed.items() if table in branch_scope}
                for ast_column in branch.columns:
                    qualifier = ast_column.table
                    if qualifier:
                        append(branch.alias_to_table.get(qualifier, qualifier), ast_column.name)
                    else:
                        table, resolved_column = self._resolve_column_reference(ast_column.name, branch_allowed)
                        if table is not None and resolved_column is not None:
                            append(table, resolved_column)
        else:
            for qualifier, column in re.findall(r"\b`?([A-Za-z_][\w]*)`?\.`?([A-Za-z_][\w]*)`?\b", sql):
                append(table_aliases.get(qualifier, qualifier), column)

            for column in self._extract_unqualified_column_references(sql, table_aliases):
                table, resolved_column = self._resolve_column_reference(column, scoped_allowed)
                if table is not None and resolved_column is not None:
                    append(table, resolved_column)

        if not columns_used and re.search(r"\*", sql):
            selected_columns = dict(context.get("selected_columns", {}))
            for table in tables_used:
                for column in list(selected_columns.get(table, [])):
                    append(str(table), str(column))

        if not columns_used:
            selected_columns = dict(context.get("selected_columns", {}))
            for table in tables_used:
                for column in list(selected_columns.get(table, [])):
                    append(str(table), str(column))

        if not columns_used:
            raise LLMOutputError("validation_failed", "Raw SQL output does not reference any selected columns.")
        return columns_used

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
        normalized: dict[str, str] = {}
        allowed = self._allowed_columns_by_table(context)
        if raw_column_types is None:
            for raw_column in columns_used:
                table, column = self._resolve_column_reference(raw_column, allowed)
                if table is None or column is None:
                    raise LLMOutputError("validation_failed", f"LLM column is not in selected schema: {raw_column}")
                canonical = f"{table}.{column}"
                expected_type = expected_types.get(canonical)
                if not expected_type:
                    raise LLMOutputError("validation_failed", f"No schema sql_type found for column: {canonical}")
                normalized[canonical] = expected_type
            return normalized

        if not isinstance(raw_column_types, Mapping):
            raise LLMOutputError("validation_failed", "LLM answer mode column_types_used must be a mapping when provided.")

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

    def _validate_sql_column_references(
        self,
        context: dict[str, Any],
        sql: str,
        *,
        tables_used: list[str],
        ast_analysis: SQLAstAnalysis | None = None,
    ) -> None:
        allowed_all = self._allowed_columns_by_table(context)
        used_scope = {str(table) for table in tables_used}
        allowed = {table: columns for table, columns in allowed_all.items() if not used_scope or table in used_scope}
        if ast_analysis is not None:
            for branch in ast_analysis.branches:
                branch_scope = {table for table in branch.tables if table in used_scope} or used_scope
                branch_allowed = {table: columns for table, columns in allowed.items() if table in branch_scope}
                for column_ref in branch.columns:
                    qualifier = column_ref.table
                    if qualifier:
                        table = branch.alias_to_table.get(qualifier, qualifier)
                        if table not in branch_allowed or column_ref.name not in branch_allowed.get(table, set()):
                            raise LLMOutputError("validation_failed", f"SQL references column outside selected schema: {qualifier}.{column_ref.name}")
                    elif self._resolve_column_reference(column_ref.name, branch_allowed) == (None, None):
                        raise LLMOutputError("validation_failed", f"SQL references column outside selected schema: {column_ref.name}")
            return

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
        for table, alias in re.findall(_TABLE_ALIAS_PATTERN, sql, flags=re.I):
            aliases[table] = table
            if alias and alias.lower() not in {"on", "where", "join", "left", "right", "inner", "limit", "group", "order"}:
                aliases[alias] = table
        return aliases

    def _extract_unqualified_column_references(self, sql: str, table_aliases: dict[str, str]) -> set[str]:
        without_strings = re.sub(r"'[^']*'|\"[^\"]*\"", " ", sql)
        without_qualified = re.sub(r"\b`?[A-Za-z_][\w]*`?\.`?[A-Za-z_][\w]*`?\b", " ", without_strings)
        without_alias_names = re.sub(r"\bas\s+`?[A-Za-z_][\w]*`?", " ", without_qualified, flags=re.I)
        tokens = set(re.findall(r"\b[A-Za-z_][\w]*\b", without_alias_names))
        keywords = {
            "select", "from", "join", "left", "right", "inner", "outer", "on", "where", "and", "or",
            "limit", "order", "group", "by", "as", "like", "in", "is", "null", "not", "with", "distinct",
            "count", "sum", "avg", "min", "max", "case", "when", "then", "else", "end", "desc", "asc",
            "union", "all",
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

        if context.get("route_id") == "approval_variety_db":
            return self._generate_approval_variety_sql(context)

        base_table = selected_tables[0]
        sql = f"FROM {base_table}"
        joined_tables = {base_table}
        pending_hints = [dict(hint) for hint in join_hints if isinstance(hint, Mapping)]
        while pending_hints:
            progressed = False
            remaining_hints: list[dict[str, Any]] = []
            for hint in pending_hints:
                left_table = str(hint.get("left_table") or "")
                right_table = str(hint.get("right_table") or "")
                if not left_table or not right_table:
                    continue
                if left_table in joined_tables and right_table not in joined_tables and right_table in selected_tables:
                    sql += (
                        f" JOIN {right_table} ON "
                        f"{left_table}.{hint['left_column']} = {right_table}.{hint['right_column']}"
                    )
                    joined_tables.add(right_table)
                    progressed = True
                    continue
                if right_table in joined_tables and left_table not in joined_tables and left_table in selected_tables:
                    sql += (
                        f" JOIN {left_table} ON "
                        f"{left_table}.{hint['left_column']} = {right_table}.{hint['right_column']}"
                    )
                    joined_tables.add(left_table)
                    progressed = True
                    continue
                if left_table not in joined_tables or right_table not in joined_tables:
                    remaining_hints.append(hint)
            if not progressed:
                break
            pending_hints = remaining_hints

        projected_columns: list[str] = []
        for table in selected_tables:
            if table not in joined_tables:
                continue
            for column in selected_columns.get(table, [])[:2]:
                projected_columns.append(f"{table}.{column}" if len(joined_tables) > 1 else str(column))
        if not projected_columns:
            projected_columns = ["*"]

        if any(keyword in user_question for keyword in ("多少", "数量", "count", "几条")):
            projected_columns = ["COUNT(*) AS total"]

        sql = f"SELECT {', '.join(projected_columns)} {sql}"

        term = self._extract_variety_search_term(user_question)
        safe_term = self._safe_search_literal(term) if term else None
        where_columns = tuple(
            f"{table}.variety_name"
            for table in joined_tables
            if "variety_name" in {str(column) for column in selected_columns.get(table, [])}
        )
        sql += self._where_search_term(safe_term, where_columns)
        return sql

    def _generate_approval_variety_sql(self, context: dict[str, Any]) -> str:
        selected_tables = [str(table) for table in context.get("selected_tables", []) if str(table).endswith("_varieties")]
        selected_columns = dict(context.get("selected_columns", {}))
        user_question = str(context.get("user_question") or "")
        normalized_question = normalize_text(user_question)
        if not selected_tables:
            return "SELECT COUNT(*) AS total FROM rice_varieties"

        entity_specs = _entity_filter_specs(context, selected_tables=selected_tables)
        if entity_specs:
            sql = self._generate_entity_approval_variety_sql(
                selected_tables=selected_tables,
                selected_columns=selected_columns,
                user_question=user_question,
                normalized_question=normalized_question,
                entity_specs=entity_specs,
                context=context,
            )
            return self._apply_query_level_constraints(sql, context)

        if len(selected_tables) > 1:
            sql = self._generate_cross_approval_variety_sql(
                selected_tables=selected_tables,
                selected_columns=selected_columns,
                user_question=user_question,
                normalized_question=normalized_question,
                context=context,
            )
            return self._apply_query_level_constraints(sql, context)

        base_table = selected_tables[0]
        if any(keyword in normalized_question for keyword in ("多少", "数量", "count", "几条")):
            where = self._where_from_constraint_fragments(context, base_table)
            return self._apply_query_level_constraints(f"SELECT COUNT(*) AS total FROM {base_table}{where}", context)

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
        where = self._append_constraint_fragments(where, self._global_filter_fragments(context, base_table, exclude_fields={"variety_name"} if safe_term else set()))
        projection = ", ".join(f"{base_table}.{column}" for column in projected) if projected != ["*"] else "*"
        if projected == ["*"]:
            sql = f"SELECT {_sql_literal(base_table)} AS source_table, {_sql_literal(_crop_for_approval_table(base_table) or '')} AS source_crop, {base_table}.* FROM {base_table}{where}"
        else:
            sql = (
                f"SELECT {_sql_literal(base_table)} AS source_table, "
                f"{_sql_literal(_crop_for_approval_table(base_table) or '')} AS source_crop, "
                f"{projection} FROM {base_table}{where}"
            )
        return self._apply_query_level_constraints(sql, context)

    def _generate_cross_approval_variety_sql(
        self,
        *,
        selected_tables: list[str],
        selected_columns: Mapping[str, Any],
        user_question: str,
        normalized_question: str,
        context: Mapping[str, Any] | None = None,
    ) -> str:
        common_columns = self._common_approval_projection_columns(selected_tables, selected_columns)
        if any(keyword in normalized_question for keyword in ("多少", "数量", "count", "几条")):
            parts = [
                f"SELECT {_sql_literal(table)} AS source_table, {_sql_literal(_crop_for_approval_table(table) or '')} AS source_crop, COUNT(*) AS total FROM {table}{self._where_from_constraint_fragments(context or {}, table)}"
                for table in selected_tables
            ]
            return " UNION ALL ".join(parts)

        term = self._extract_variety_search_term(user_question)
        safe_term = self._safe_search_literal(term) if term else None
        parts: list[str] = []
        for table in selected_tables:
            available = {str(column) for column in list(selected_columns.get(table, []))}
            projected = [column for column in common_columns if column in available]
            if not projected:
                projected = [str(column) for column in list(selected_columns.get(table, []))[:5]]
            projection = ", ".join(f"{table}.{column}" for column in projected) if projected else "*"
            where = self._where_search_term(safe_term, (f"{table}.variety_name",)) if "variety_name" in available else ""
            where = self._append_constraint_fragments(where, self._global_filter_fragments(context or {}, table, exclude_fields={"variety_name"} if safe_term else set()))
            if projection == "*":
                select_projection = (
                    f"{_sql_literal(table)} AS source_table, "
                    f"{_sql_literal(_crop_for_approval_table(table) or '')} AS source_crop, "
                    f"{table}.*"
                )
            else:
                select_projection = (
                    f"{_sql_literal(table)} AS source_table, "
                    f"{_sql_literal(_crop_for_approval_table(table) or '')} AS source_crop, "
                    f"{projection}"
                )
            parts.append(f"SELECT {select_projection} FROM {table}{where}")
        return " UNION ALL ".join(parts)

    def _generate_entity_approval_variety_sql(
        self,
        *,
        selected_tables: list[str],
        selected_columns: Mapping[str, Any],
        user_question: str,
        normalized_question: str,
        entity_specs: list[dict[str, str]],
        context: Mapping[str, Any] | None = None,
    ) -> str:
        count_mode = any(keyword in normalized_question for keyword in ("多少", "数量", "count", "几条"))
        detail_requested = any(
            keyword in normalized_question
            for keyword in ("详细", "详情", "所有信息", "全部信息", "完整", "具体", "介绍", "信息")
        )
        common_columns = self._common_approval_projection_columns(selected_tables, selected_columns) if len(selected_tables) > 1 else []
        parts: list[str] = []
        seen: set[tuple[str, str, str, str]] = set()
        for spec in entity_specs:
            table = spec["table"]
            field = spec["field"]
            tier = spec["tier"]
            entity_text = spec["entity_text"]
            if table not in selected_tables:
                continue
            available = {str(column) for column in list(selected_columns.get(table, []))}
            if field not in available:
                continue
            safe_term = self._safe_search_literal(entity_text)
            if not safe_term:
                continue
            key = (table, field, tier, safe_term)
            if key in seen:
                continue
            seen.add(key)
            source_projection = (
                f"{_sql_literal(table)} AS source_table, "
                f"{_sql_literal(_crop_for_approval_table(table) or '')} AS source_crop, "
                f"{_sql_literal(field)} AS matched_field, "
                f"{_sql_literal(tier)} AS match_tier"
            )
            if count_mode:
                projection = f"{source_projection}, COUNT(*) AS total"
            else:
                if common_columns:
                    projected = [column for column in common_columns if column in available]
                else:
                    preferred = _APPROVAL_DETAIL_COLUMNS if detail_requested else _APPROVAL_LIST_COLUMNS
                    projected = [column for column in preferred if column in available]
                if not projected:
                    projected = [str(column) for column in list(selected_columns.get(table, []))[:5]]
                projection_columns = ", ".join(f"{table}.{column}" for column in projected) if projected else f"{table}.*"
                projection = f"{source_projection}, {projection_columns}"
            where = self._append_constraint_fragments(
                f" WHERE {table}.{field} LIKE '%{safe_term}%'",
                self._global_filter_fragments(context or {}, table, exclude_fields={field}),
            )
            parts.append(f"SELECT {projection} FROM {table}{where}")
        if parts:
            return " UNION ALL ".join(parts)
        return self._generate_cross_approval_variety_sql(
            selected_tables=selected_tables,
            selected_columns=selected_columns,
            user_question=user_question,
            normalized_question=normalized_question,
            context=context,
        )

    def _common_approval_projection_columns(
        self,
        selected_tables: list[str],
        selected_columns: Mapping[str, Any],
    ) -> list[str]:
        common: set[str] | None = None
        for table in selected_tables:
            columns = {str(column) for column in list(selected_columns.get(table, []))}
            common = columns if common is None else common & columns
        available_common = common or set()
        preferred = [
            column
            for column in _APPROVAL_LIST_COLUMNS
            if column in available_common
        ]
        return preferred or sorted(available_common)[:6]

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

    def _where_search_term(self, safe_term: str | None, columns: tuple[str, ...]) -> str:
        if not safe_term:
            return ""
        clauses = [f"{column} LIKE '%{safe_term}%'" for column in columns]
        return " WHERE " + " OR ".join(clauses)

    def _validate_constraint_coverage(
        self,
        context: dict[str, Any],
        sql: str,
        *,
        tables_used: list[str],
        ast_analysis: SQLAstAnalysis | None = None,
    ) -> dict[str, Any]:
        query_constraints = context.get("query_constraints")
        if not isinstance(query_constraints, Mapping):
            return {"covered": True, "checked_constraints": 0, "summary": "no query constraints"}
        required = [item for item in list(query_constraints.get("required_constraints") or []) if isinstance(item, Mapping)]
        groups = [group for group in list(query_constraints.get("constraint_groups") or []) if isinstance(group, Mapping)]
        if not required and not groups:
            return {"covered": True, "checked_constraints": 0, "summary": "no required query constraints"}

        branches = _split_union_all_branches(sql) or [sql]
        if ast_analysis is None:
            checked = 0
            for item in required:
                scope = str(item.get("scope") or "global_filter")
                operator = str(item.get("operator") or "").upper()
                if scope == "global_filter":
                    for branch in branches:
                        table = _first_branch_table(branch, tables_used)
                        if not table or not self._constraint_applies_to_table(item, table):
                            continue
                        checked += 1
                        if not _branch_covers_constraint(branch, item, table=table):
                            raise LLMOutputError("validation_failed", f"SQL does not cover required constraint: {item.get('id')}")
                elif scope == "aggregate" and operator == "COUNT":
                    checked += 1
                    if not re.search(r"\bcount\s*\(\s*\*\s*\)", sql, flags=re.I):
                        raise LLMOutputError("validation_failed", f"SQL does not cover COUNT constraint: {item.get('id')}")
                elif scope == "query_level":
                    checked += 1
                    if not _sql_covers_query_level_constraint(sql, item):
                        raise LLMOutputError("validation_failed", f"SQL does not cover query-level constraint: {item.get('id')}")
                elif scope == "branch_filter":
                    if not any(_branch_covers_constraint(branch, item, table=_first_branch_table(branch, tables_used) or "") for branch in branches):
                        raise LLMOutputError("validation_failed", f"SQL does not cover branch constraint: {item.get('id')}")
                    checked += 1

            by_id = {str(item.get("id")): item for item in required}
            for group in groups:
                if str(group.get("mode")) != "branch_union":
                    continue
                members = [by_id.get(member) for member in list(group.get("members") or [])]
                members = [member for member in members if isinstance(member, Mapping)]
                if len(members) < 2:
                    continue
                if not re.search(r"\bunion\s+all\b", sql, flags=re.I):
                    raise LLMOutputError("validation_failed", f"branch_union group requires UNION ALL: {group.get('id')}")
                for member in members:
                    checked += 1
                    if not any(_branch_covers_group_member(branch, member, tables_used=tables_used) for branch in branches):
                        raise LLMOutputError("validation_failed", f"branch_union member is not traceable: {member.get('id')}")

            return {
                "covered": True,
                "checked_constraints": checked,
                "summary": query_constraints.get("constraint_summary") or "required constraints covered",
            }

        ast_branches = list(ast_analysis.branches)
        checked = 0
        for item in required:
            scope = str(item.get("scope") or "global_filter")
            operator = str(item.get("operator") or "").upper()
            if scope == "global_filter":
                for branch in ast_branches:
                    table = _first_ast_branch_table(branch, tables_used)
                    if not table or not self._constraint_applies_to_table(item, table):
                        continue
                    checked += 1
                    field = _constraint_field_for_table(item, table)
                    if not branch_has_constraint(branch, field=field, operator=operator, value=item.get("value"), table=table):
                        raise LLMOutputError("validation_failed", f"SQL does not cover required constraint: {item.get('id')}")
            elif scope == "aggregate" and operator == "COUNT":
                checked += 1
                if not any(branch_has_count(branch) for branch in ast_branches):
                    raise LLMOutputError("validation_failed", f"SQL does not cover COUNT constraint: {item.get('id')}")
            elif scope == "query_level":
                checked += 1
                if not _ast_covers_query_level_constraint(ast_analysis, item):
                    raise LLMOutputError("validation_failed", f"SQL does not cover query-level constraint: {item.get('id')}")
            elif scope == "branch_filter":
                # Branch groups are checked below; standalone branch filters are accepted when any traceable branch covers them.
                if not any(_ast_branch_covers_constraint(branch, item, tables_used=tables_used) for branch in ast_branches):
                    raise LLMOutputError("validation_failed", f"SQL does not cover branch constraint: {item.get('id')}")
                checked += 1

        by_id = {str(item.get("id")): item for item in required}
        for group in groups:
            if str(group.get("mode")) != "branch_union":
                continue
            members = [by_id.get(member) for member in list(group.get("members") or [])]
            members = [member for member in members if isinstance(member, Mapping)]
            if len(members) < 2:
                continue
            if not ast_analysis.is_union_all:
                raise LLMOutputError("validation_failed", f"branch_union group requires UNION ALL: {group.get('id')}")
            for member in members:
                checked += 1
                if not any(_ast_branch_covers_group_member(branch, member, tables_used=tables_used) for branch in ast_branches):
                    raise LLMOutputError("validation_failed", f"branch_union member is not traceable: {member.get('id')}")

        return {
            "covered": True,
            "checked_constraints": checked,
            "summary": query_constraints.get("constraint_summary") or "required constraints covered",
        }

    def _constraint_applies_to_table(self, item: Mapping[str, Any], table: str) -> bool:
        tables = [str(value) for value in list(item.get("tables") or [])]
        return not tables or table in tables

    def _global_filter_fragments(self, context: Mapping[str, Any], table: str, *, exclude_fields: set[str] | None = None) -> list[str]:
        exclude_fields = exclude_fields or set()
        query_constraints = context.get("query_constraints") if isinstance(context, Mapping) else None
        if not isinstance(query_constraints, Mapping):
            return []
        fragments: list[str] = []
        for item in list(query_constraints.get("required_constraints") or []):
            if not isinstance(item, Mapping):
                continue
            if str(item.get("scope") or "") != "global_filter":
                continue
            if not self._constraint_applies_to_table(item, table):
                continue
            field = _constraint_field_for_table(item, table)
            if not field or field in exclude_fields:
                continue
            fragment = _constraint_to_sql_fragment(item, table=table, field=field)
            if fragment and fragment not in fragments:
                fragments.append(fragment)
        return fragments

    def _where_from_constraint_fragments(self, context: Mapping[str, Any], table: str) -> str:
        fragments = self._global_filter_fragments(context, table)
        return " WHERE " + " AND ".join(fragments) if fragments else ""

    def _append_constraint_fragments(self, where: str, fragments: list[str]) -> str:
        if not fragments:
            return where
        if where.strip():
            clause = where.strip()
            if clause.lower().startswith("where "):
                clause = clause[6:]
            return " WHERE (" + clause + ") AND " + " AND ".join(fragments)
        return " WHERE " + " AND ".join(fragments)

    def _apply_query_level_constraints(self, sql: str, context: Mapping[str, Any]) -> str:
        query_constraints = context.get("query_constraints") if isinstance(context, Mapping) else None
        if not isinstance(query_constraints, Mapping):
            return sql
        order_clause = ""
        limit_clause = ""
        has_count = False
        for item in list(query_constraints.get("required_constraints") or []):
            if not isinstance(item, Mapping):
                continue
            operator = str(item.get("operator") or "").upper()
            if str(item.get("scope") or "") == "aggregate" and operator == "COUNT":
                has_count = True
            if str(item.get("scope") or "") != "query_level":
                continue
            if operator == "ORDER_BY" and str(item.get("field") or "") == "year":
                direction = "DESC"
                value = item.get("value")
                if isinstance(value, Mapping) and str(value.get("direction") or "").upper() in {"ASC", "DESC"}:
                    direction = str(value.get("direction")).upper()
                order_clause = f" ORDER BY year {direction}"
            elif operator == "LIMIT":
                try:
                    limit = int(item.get("value"))
                except (TypeError, ValueError):
                    continue
                if limit > 0:
                    limit_clause = f" LIMIT {limit}"
        if has_count or (not order_clause and not limit_clause):
            return sql
        if re.search(r"\bunion\s+all\b", sql, flags=re.I):
            return f"SELECT * FROM ({sql}) AS constrained_results{order_clause}{limit_clause}"
        result = sql
        if order_clause and not re.search(r"\border\s+by\b", result, flags=re.I):
            result += order_clause
        if limit_clause and not re.search(r"\blimit\s+\d+", result, flags=re.I):
            result += limit_clause
        return result

    def _validate_variety_name_matching_policy(self, sql: str) -> None:
        left_strict_equal = r"(?:`?[A-Za-z_][\w]*`?\.)?`?variety_name`?\s*=\s*(?:'[^']*'|\"[^\"]*\"|`?[A-Za-z_][\w]*`?(?:\.`?[A-Za-z_][\w]*`?)?)"
        right_strict_equal = r"(?:'[^']*'|\"[^\"]*\")\s*=\s*(?:`?[A-Za-z_][\w]*`?\.)?`?variety_name`?"
        if re.search(left_strict_equal, sql, flags=re.I) or re.search(right_strict_equal, sql, flags=re.I):
            raise LLMOutputError(
                "validation_failed",
                "SQL must use LIKE instead of strict equality when filtering variety_name.",
            )

    def _validate_source_projection_policy(self, context: dict[str, Any], sql: str, *, tables_used: list[str]) -> None:
        if context.get("route_id") != "approval_variety_db":
            return
        if len({str(table) for table in tables_used if str(table).endswith("_varieties")}) <= 1:
            return
        if not re.search(r"\bsource_table\b", sql, flags=re.I) or not re.search(r"\bsource_crop\b", sql, flags=re.I):
            raise LLMOutputError(
                "validation_failed",
                "Cross-table approval variety SQL must project source_table and source_crop.",
            )

    def _validate_entity_filter_policy(self, context: dict[str, Any], sql: str, *, ast_analysis: SQLAstAnalysis | None = None) -> None:
        if context.get("route_id") != "approval_variety_db":
            return
        specs = _entity_filter_specs(context)
        if not specs:
            return
        if ast_analysis is not None:
            for spec in specs:
                if not any(_ast_branch_matches_entity_spec(branch, spec) for branch in ast_analysis.branches):
                    raise LLMOutputError(
                        "validation_failed",
                        f"Entity-aware approval SQL must preserve branch source marker for {spec['field']}:{spec['tier']}.",
                    )
            if len(specs) > 1 and not ast_analysis.is_union_all:
                raise LLMOutputError(
                    "validation_failed",
                    "Entity-aware approval SQL with multiple matched fields must use UNION ALL branches.",
                )
            return
        for spec in specs:
            field = re.escape(spec["field"])
            if not re.search(rf"\b`?{field}`?\b\s+like\b", sql, flags=re.I):
                raise LLMOutputError(
                    "validation_failed",
                    f"Entity-aware approval SQL must include LIKE filter for {spec['field']}.",
                )
        if not re.search(r"\bmatched_field\b", sql, flags=re.I) or not re.search(r"\bmatch_tier\b", sql, flags=re.I):
            raise LLMOutputError(
                "validation_failed",
                "Entity-aware approval SQL must project matched_field and match_tier.",
            )
        if len(specs) > 1 and not re.search(r"\bunion\s+all\b", sql, flags=re.I):
            raise LLMOutputError(
                "validation_failed",
                "Entity-aware approval SQL with multiple matched fields must use UNION ALL branches.",
            )
        branches = _split_union_all_branches(sql)
        for spec in specs:
            if not any(_branch_matches_entity_spec(branch, spec) for branch in branches):
                raise LLMOutputError(
                    "validation_failed",
                    f"Entity-aware approval SQL must preserve branch source marker for {spec['field']}:{spec['tier']}.",
                )

    @staticmethod
    def _should_defer_validation_to_engine(request: CapabilityExecutionRequest, exc: LLMOutputError) -> bool:
        if exc.reason != "validation_failed":
            return False
        return request.metadata.get("component") == "sql_generate" or bool(request.metadata.get("sqlquery_engine_repair_enabled"))

    def _best_effort_failed_sql(self, raw_output: str, llm_payload: Mapping[str, Any] | None) -> str:
        if isinstance(llm_payload, Mapping):
            sql = str(llm_payload.get("sql") or "").strip()
            if sql:
                return sql.rstrip(";").strip()
        try:
            return self._extract_sql_from_text(raw_output)
        except LLMOutputError:
            return ""


_APPROVAL_TABLE_TO_CROP = {
    "corn_varieties": "corn",
    "rice_varieties": "rice",
    "cotton_varieties": "cotton",
    "wheat_varieties": "wheat",
    "soybean_varieties": "soybean",
}


def _crop_for_approval_table(table: str) -> str | None:
    return _APPROVAL_TABLE_TO_CROP.get(str(table))


def _source_scope_from_tables(tables: list[str], *, context: Mapping[str, Any] | None = None) -> dict[str, Any]:
    normalized = [str(table) for table in tables]
    payload = {
        "tables_used": normalized,
        "approval_crops": [
            crop
            for table in normalized
            for crop in [_crop_for_approval_table(table)]
            if crop
        ],
    }
    if context is not None:
        if context.get("matched_fields"):
            payload["matched_fields"] = list(context.get("matched_fields") or [])
        if context.get("match_tiers"):
            payload["match_tiers"] = list(context.get("match_tiers") or [])
    return payload


def _entity_filter_specs(context: Mapping[str, Any], *, selected_tables: list[str] | None = None) -> list[dict[str, str]]:
    selected = {str(table) for table in (selected_tables or list(context.get("selected_tables", [])))}
    specs: list[dict[str, str]] = []
    seen: set[tuple[str, str, str, str]] = set()

    def add(*, table: str, field: str, tier: str, entity_text: str) -> None:
        if not table or not field or not entity_text:
            return
        if selected and table not in selected:
            return
        if field not in {"variety_name", "applicant", "breeder", "approval_num"}:
            return
        key = (table, field, tier, entity_text)
        if key in seen:
            return
        specs.append({"table": table, "field": field, "tier": tier, "entity_text": entity_text})
        seen.add(key)

    match_summary = context.get("match_summary")
    if isinstance(match_summary, Mapping):
        for tier in ("primary", "secondary", "peer"):
            raw_entries = match_summary.get(tier)
            if not isinstance(raw_entries, list | tuple):
                continue
            for entry in raw_entries:
                if not isinstance(entry, Mapping):
                    continue
                add(
                    table=str(entry.get("table") or ""),
                    field=str(entry.get("field") or ""),
                    tier=tier,
                    entity_text=str(entry.get("entity_text") or entry.get("text") or "").strip(),
                )

    query_constraints = context.get("query_constraints")
    if isinstance(query_constraints, Mapping):
        for item in list(query_constraints.get("required_constraints") or []):
            if not isinstance(item, Mapping):
                continue
            if str(item.get("scope") or "") != "branch_filter":
                continue
            field = str(item.get("field") or "")
            value = str(item.get("value") or "").strip()
            tier = str(item.get("match_tier") or "peer")
            for table in list(item.get("tables") or selected):
                add(table=str(table), field=field, tier=tier, entity_text=value)
    return specs


def _split_union_all_branches(sql: str) -> list[str]:
    return [branch.strip() for branch in re.split(r"\bunion\s+all\b", sql, flags=re.I) if branch.strip()]


def _branch_matches_entity_spec(branch: str, spec: Mapping[str, str]) -> bool:
    table = re.escape(str(spec.get("table") or ""))
    field = re.escape(str(spec.get("field") or ""))
    tier = re.escape(str(spec.get("tier") or ""))
    if not table or not field or not tier:
        return False
    has_table = bool(re.search(rf"\bfrom\s+`?{table}`?\b", branch, flags=re.I))
    has_field_marker = bool(re.search(rf"[\'\"]{field}[\'\"]\s+as\s+`?matched_field`?", branch, flags=re.I))
    has_tier_marker = bool(re.search(rf"[\'\"]{tier}[\'\"]\s+as\s+`?match_tier`?", branch, flags=re.I))
    has_field_filter = bool(
        re.search(
            rf"(?:`?{table}`?\.)?`?{field}`?\s+like\b",
            branch,
            flags=re.I,
        )
    )
    return has_table and has_field_marker and has_tier_marker and has_field_filter


def _first_branch_table(branch: str, tables_used: list[str]) -> str | None:
    aliases = re.findall(_TABLE_ALIAS_PATTERN, branch, flags=re.I)
    used = {str(table) for table in tables_used}
    for table, _alias in aliases:
        if not used or table in used:
            return str(table)
    for table in tables_used:
        if re.search(rf"\b`?{re.escape(str(table))}`?\b", branch, flags=re.I):
            return str(table)
    return None


def _constraint_field_for_table(item: Mapping[str, Any], table: str) -> str:
    field_by_table = item.get("field_by_table")
    if isinstance(field_by_table, Mapping) and field_by_table.get(table):
        return str(field_by_table[table])
    return str(item.get("field") or "")


def _constraint_to_sql_fragment(item: Mapping[str, Any], *, table: str, field: str) -> str:
    operator = str(item.get("operator") or "").upper()
    column = f"{table}.{field}" if field and field != "*" else field
    value = item.get("value")
    if operator == "=":
        return f"{column} = {_sql_value(value)}"
    if operator == ">=":
        return f"{column} >= {_sql_value(value)}"
    if operator == "<=":
        return f"{column} <= {_sql_value(value)}"
    if operator == "BETWEEN" and isinstance(value, list | tuple) and len(value) == 2:
        return f"{column} BETWEEN {_sql_value(value[0])} AND {_sql_value(value[1])}"
    if operator == "LIKE":
        safe = str(value or "").replace("'", "''")
        return f"{column} LIKE '%{safe}%'"
    return ""


def _sql_value(value: Any) -> str:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return str(value)
    return _sql_literal(str(value))


def _branch_covers_constraint(branch: str, item: Mapping[str, Any], *, table: str) -> bool:
    field = _constraint_field_for_table(item, table)
    if not field:
        return False
    operator = str(item.get("operator") or "").upper()
    value = item.get("value")
    column_pattern = rf"(?:`?{re.escape(table)}`?\.)?`?{re.escape(field)}`?"
    if operator == "=":
        return bool(re.search(column_pattern + rf"\s*=\s*{re.escape(str(value))}\b", branch, flags=re.I))
    if operator in {">=", "<="}:
        return bool(re.search(column_pattern + rf"\s*{re.escape(operator)}\s*{re.escape(str(value))}\b", branch, flags=re.I))
    if operator == "BETWEEN" and isinstance(value, list | tuple) and len(value) == 2:
        return bool(re.search(column_pattern + rf"\s+between\s+{re.escape(str(value[0]))}\s+and\s+{re.escape(str(value[1]))}\b", branch, flags=re.I))
    if operator == "LIKE":
        needle = re.escape(str(value))
        return bool(re.search(column_pattern + rf"\s+like\s+[\'\"]%[^\'\"]*{needle}[^\'\"]*%[\'\"]", branch, flags=re.I))
    return False


def _branch_covers_group_member(branch: str, item: Mapping[str, Any], *, tables_used: list[str]) -> bool:
    table = _first_branch_table(branch, tables_used)
    if not table or not _branch_covers_constraint(branch, item, table=table):
        return False
    field = re.escape(str(item.get("field") or ""))
    tier = re.escape(str(item.get("match_tier") or ""))
    if not field or not tier:
        return False
    has_field_marker = bool(re.search(rf"[\'\"]{field}[\'\"]\s+as\s+`?matched_field`?", branch, flags=re.I))
    has_tier_marker = bool(re.search(rf"[\'\"]{tier}[\'\"]\s+as\s+`?match_tier`?", branch, flags=re.I))
    return has_field_marker and has_tier_marker


def _analyze_generated_sql(sql: str) -> SQLAstAnalysis:
    try:
        return analyze_sql(sql)
    except SQLAstError as exc:
        raise LLMOutputError("validation_failed", f"SQL parse failed: {exc}") from exc


def _first_ast_branch_table(branch: SQLAstBranch, tables_used: list[str]) -> str | None:
    used = {str(table) for table in tables_used}
    for table in branch.tables:
        if not used or table in used:
            return table
    return None


def _ast_branch_covers_constraint(branch: SQLAstBranch, item: Mapping[str, Any], *, tables_used: list[str]) -> bool:
    table = _first_ast_branch_table(branch, tables_used)
    if not table:
        return False
    field = _constraint_field_for_table(item, table)
    if not field:
        return False
    return branch_has_constraint(
        branch,
        field=field,
        operator=str(item.get("operator") or ""),
        value=item.get("value"),
        table=table,
    )


def _ast_branch_covers_group_member(branch: SQLAstBranch, item: Mapping[str, Any], *, tables_used: list[str]) -> bool:
    if not _ast_branch_covers_constraint(branch, item, tables_used=tables_used):
        return False
    field = str(item.get("field") or "")
    tier = str(item.get("match_tier") or "")
    return branch_projection_literal(branch, "matched_field") == field and branch_projection_literal(branch, "match_tier") == tier


def _ast_branch_matches_entity_spec(branch: SQLAstBranch, spec: Mapping[str, str]) -> bool:
    table = str(spec.get("table") or "")
    field = str(spec.get("field") or "")
    tier = str(spec.get("tier") or "")
    value = str(spec.get("entity_text") or "")
    if not table or not field or not tier or not value:
        return False
    if not branch_has_constraint(branch, field=field, operator="LIKE", value=value, table=table):
        return False
    return branch_projection_literal(branch, "matched_field") == field and branch_projection_literal(branch, "match_tier") == tier


def _ast_covers_query_level_constraint(analysis: SQLAstAnalysis, item: Mapping[str, Any]) -> bool:
    operator = str(item.get("operator") or "").upper()
    if operator == "ORDER_BY":
        field = str(item.get("field") or "")
        direction = "DESC"
        value = item.get("value")
        if isinstance(value, Mapping) and str(value.get("direction") or "").upper() in {"ASC", "DESC"}:
            direction = str(value.get("direction")).upper()
        return final_has_order(analysis, field=field, direction=direction)
    if operator == "LIMIT":
        try:
            limit = int(item.get("value"))
        except (TypeError, ValueError):
            return False
        return final_has_limit(analysis, limit)
    return True


def _sql_covers_query_level_constraint(sql: str, item: Mapping[str, Any]) -> bool:
    operator = str(item.get("operator") or "").upper()
    last_union = max((match.start() for match in re.finditer(r"\bunion\s+all\b", sql, flags=re.I)), default=-1)
    if operator == "ORDER_BY":
        field = re.escape(str(item.get("field") or ""))
        direction = "DESC"
        value = item.get("value")
        if isinstance(value, Mapping) and str(value.get("direction") or "").upper() in {"ASC", "DESC"}:
            direction = str(value.get("direction")).upper()
        match = re.search(rf"\border\s+by\s+(?:`?[A-Za-z_][\w]*`?\.)?`?{field}`?\s+{direction.lower()}\b", sql, flags=re.I)
        return bool(match and match.start() > last_union)
    if operator == "LIMIT":
        try:
            limit = int(item.get("value"))
        except (TypeError, ValueError):
            return False
        match = re.search(rf"\blimit\s+{limit}\b", sql, flags=re.I)
        return bool(match and match.start() > last_union)
    return True


def _sql_literal(value: str) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "''") + "'"


def _repair_context_from_request(request: CapabilityExecutionRequest) -> Mapping[str, Any] | None:
    value = request.input_payload.get("sql_repair_context")
    if isinstance(value, Mapping):
        return value
    value = request.metadata.get("sql_repair_context")
    if isinstance(value, Mapping):
        return value
    return None


def _query_constraint_clarification(context: Mapping[str, Any]) -> Mapping[str, Any] | None:
    query_constraints = context.get("query_constraints")
    if not isinstance(query_constraints, Mapping):
        return None
    clarification = query_constraints.get("clarification_needed")
    return clarification if isinstance(clarification, Mapping) and clarification else None
