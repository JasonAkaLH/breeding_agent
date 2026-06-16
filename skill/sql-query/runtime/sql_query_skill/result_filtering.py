from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from src.core.contracts import CapabilityContract, CapabilityExecutionRequest, CapabilityExecutionResult
from src.integrations.token_counter import get_num_of_tokens_from_text

from .helpers import SQL_QUERY_AUDIT_LLM_CALL_EVENT, SQL_QUERY_AUDIT_LLM_FALLBACK_EVENT, SQL_QUERY_PUBLIC_CAPABILITY_ID, find_dependency_output, make_artifact, make_audit_event
from .llm_utils import LLMOutputError, TextGenerator, call_text_generator, json_ready, parse_json_object
from .prompt_builders import (
    build_result_filtering_prompt,
    build_result_filtering_prompt_payload,
)


_VARIETY_LIKE_PATTERN = re.compile(
    r"(?:`?[A-Za-z_][\w]*`?\.)?`?variety_name`?\s+LIKE\s+['\"]%([^%'\";]+)%['\"]",
    flags=re.IGNORECASE,
)
_ORGANIZATION_LIKE_PATTERN = re.compile(
    r"(?:`?[A-Za-z_][\w]*`?\.)?`?(?:applicant|breeder)`?\s+LIKE\s+['\"]%([^%'\";]+)%['\"]",
    flags=re.IGNORECASE,
)
_ORGANIZATION_ROW_KEYS = frozenset({
    "applicant",
    "breeder",
    "申请者",
    "育种者",
})

_TOKEN_BUDGET_TOO_SMALL_MESSAGE = "查询结果内容过长，当前无法整理成可靠总结。请缩小查询范围后重试。"


@dataclass(frozen=True)
class _TokenTrimResult:
    rows: list[dict[str, Any]]
    applied: bool
    trim_max_tokens: int | None
    full_token_num: int | None
    trimmed_token_num: int | None
    removed_row_count: int


class SQLQueryResultFilteringCapability(CapabilityContract):
    capability_id = SQL_QUERY_PUBLIC_CAPABILITY_ID
    version = "1"
    description = "从 LIKE 宽召回的 SQLQuery 候选行中筛选符合用户需求的表格行。"

    def __init__(
        self,
        *,
        llm_text_generator: TextGenerator | None = None,
        trim_max_tokens: int | None = None,
    ) -> None:
        self._llm_text_generator = llm_text_generator
        self._trim_max_tokens = trim_max_tokens if trim_max_tokens is not None and trim_max_tokens >= 0 else None

    async def execute(self, request: CapabilityExecutionRequest) -> CapabilityExecutionResult:
        upstream = find_dependency_output(request, ("rows", "columns", "row_count"))
        question_context = self._find_optional_dependency_output(
            request,
            ("user_question", "route_id", "schema_profile_id"),
        )
        source_context = {**dict(upstream), **dict(question_context)}
        raw_rows = self._normalize_rows(upstream.get("rows", []))
        raw_source_row_count = self._int_or_default(
            upstream.get("source_row_count"),
            self._int_or_default(upstream.get("row_count"), len(raw_rows)),
        )
        llm_enabled = raw_source_row_count > 0 and self._llm_text_generator is not None
        token_trim = self._trim_rows_for_llm(raw_rows) if llm_enabled else self._no_token_trim(raw_rows)
        source_rows = token_trim.rows
        filter_upstream = dict(upstream)
        filter_upstream["rows"] = source_rows
        filter_upstream["row_count"] = len(source_rows)
        filter_upstream["source_row_count"] = raw_source_row_count
        if token_trim.applied:
            filter_upstream["truncated"] = True

        prompt_payload = build_result_filtering_prompt_payload(
            filter_upstream,
            question_context=question_context,
        )
        columns = [str(column) for column in list(upstream.get("columns", []))]
        source_row_count = int(prompt_payload["result_context"]["source_row_count"])
        source_preview_row_count = len(raw_rows)
        candidate_row_count = int(prompt_payload["result_context"]["candidate_row_count"])
        truncated = bool(prompt_payload["result_context"]["truncated"] or upstream.get("truncated", False))
        domain_keep_indexes, domain_filter_reason = self._specific_variety_keep_indexes(
            rows=source_rows,
            upstream=filter_upstream,
            question_context=question_context,
        )
        protected_keep_indexes, protected_filter_reason = (None, None)
        if domain_keep_indexes is None:
            protected_keep_indexes, protected_filter_reason = self._organization_like_protected_indexes(
                rows=source_rows,
                upstream=filter_upstream,
                question_context=question_context,
            )
        domain_filter_applied = domain_keep_indexes is not None or protected_keep_indexes is not None
        combined_domain_filter_reason = self._join_filter_reasons(domain_filter_reason, protected_filter_reason)

        if raw_source_row_count > 0 and token_trim.applied and not source_rows:
            return self._success_result(
                request,
                columns=columns,
                rows=[],
                kept_row_indexes=[],
                filter_source="deterministic",
                fallback_used=False,
                fallback_reason=None,
                source_row_count=source_row_count,
                source_preview_row_count=source_preview_row_count,
                candidate_row_count=candidate_row_count,
                truncated=True,
                route_id=question_context.get("route_id") or upstream.get("route_id"),
                schema_profile_id=question_context.get("schema_profile_id") or upstream.get("schema_profile_id"),
                source_context=source_context,
                filter_reason=_TOKEN_BUDGET_TOO_SMALL_MESSAGE,
                domain_filter_applied=False,
                domain_filter_reason=None,
                token_trim=token_trim,
                events=(),
            )

        if source_row_count == 0 or self._llm_text_generator is None:
            kept_row_indexes = domain_keep_indexes if domain_keep_indexes is not None else list(range(len(source_rows)))
            filtered_rows = [source_rows[index] for index in kept_row_indexes]
            return self._success_result(
                request,
                columns=columns,
                rows=filtered_rows,
                kept_row_indexes=kept_row_indexes,
                filter_source="deterministic",
                fallback_used=False,
                fallback_reason=None,
                source_row_count=source_row_count,
                source_preview_row_count=source_preview_row_count,
                candidate_row_count=candidate_row_count,
                truncated=truncated,
                route_id=question_context.get("route_id") or upstream.get("route_id"),
                schema_profile_id=question_context.get("schema_profile_id") or upstream.get("schema_profile_id"),
                source_context=source_context,
                filter_reason=domain_filter_reason or ("未配置 LLM 筛选器，保留 SQL 查询返回的候选表格。" if source_row_count else "查询未返回候选行。"),
                domain_filter_applied=domain_filter_applied,
                domain_filter_reason=combined_domain_filter_reason,
                token_trim=token_trim,
                events=(),
            )

        prompt = build_result_filtering_prompt(
            filter_upstream,
            question_context=question_context,
        )
        try:
            raw_output = await call_text_generator(self._llm_text_generator, prompt, request=request)
            llm_payload = parse_json_object(raw_output)
            kept_row_indexes = self._parse_keep_row_indexes(llm_payload, candidate_row_count=candidate_row_count)
            kept_row_indexes = self._apply_domain_keep_indexes(kept_row_indexes, domain_keep_indexes=domain_keep_indexes)
            kept_row_indexes = self._apply_protected_keep_indexes(
                kept_row_indexes,
                protected_keep_indexes=protected_keep_indexes,
            )
            filtered_rows = [source_rows[index] for index in kept_row_indexes]
        except LLMOutputError as exc:
            kept_row_indexes = domain_keep_indexes if domain_keep_indexes is not None else list(range(len(source_rows)))
            fallback_rows = [source_rows[index] for index in kept_row_indexes]
            return self._fallback_result(
                request,
                columns=columns,
                rows=fallback_rows,
                kept_row_indexes=kept_row_indexes,
                fallback_reason=exc.reason,
                diagnostic=str(exc),
                source_row_count=source_row_count,
                source_preview_row_count=source_preview_row_count,
                candidate_row_count=candidate_row_count,
                truncated=truncated,
                route_id=question_context.get("route_id") or upstream.get("route_id"),
                schema_profile_id=question_context.get("schema_profile_id") or upstream.get("schema_profile_id"),
                source_context=source_context,
                domain_filter_applied=domain_filter_applied,
                domain_filter_reason=combined_domain_filter_reason,
                token_trim=token_trim,
            )
        except Exception as exc:
            kept_row_indexes = domain_keep_indexes if domain_keep_indexes is not None else list(range(len(source_rows)))
            fallback_rows = [source_rows[index] for index in kept_row_indexes]
            return self._fallback_result(
                request,
                columns=columns,
                rows=fallback_rows,
                kept_row_indexes=kept_row_indexes,
                fallback_reason="provider_failed",
                diagnostic=str(exc),
                source_row_count=source_row_count,
                source_preview_row_count=source_preview_row_count,
                candidate_row_count=candidate_row_count,
                truncated=truncated,
                route_id=question_context.get("route_id") or upstream.get("route_id"),
                schema_profile_id=question_context.get("schema_profile_id") or upstream.get("schema_profile_id"),
                source_context=source_context,
                domain_filter_applied=domain_filter_applied,
                domain_filter_reason=combined_domain_filter_reason,
                token_trim=token_trim,
            )

        event = make_audit_event(
            request,
            event_type=SQL_QUERY_AUDIT_LLM_CALL_EVENT,
            payload={
                "capability_id": self.capability_id, "stage": "result_filtering",
                "status": "succeeded",
                "filter_source": "llm",
                "fallback_used": False,
                "prompt_recorded": False,
                "rows_recorded": "candidate_only",
                "source_row_count": source_row_count,
                "candidate_row_count": candidate_row_count,
                "filtered_row_count": len(filtered_rows),
                "removed_row_count": max(source_row_count - len(filtered_rows), 0),
                "truncated": truncated,
                "token_trim_applied": token_trim.applied,
                "token_trim_removed_row_count": token_trim.removed_row_count,
            },
        )
        return self._success_result(
            request,
            columns=columns,
            rows=filtered_rows,
            kept_row_indexes=kept_row_indexes,
            filter_source="llm",
            fallback_used=False,
            fallback_reason=None,
            source_row_count=source_row_count,
            source_preview_row_count=source_preview_row_count,
            candidate_row_count=candidate_row_count,
            truncated=truncated,
            route_id=question_context.get("route_id") or upstream.get("route_id"),
            schema_profile_id=question_context.get("schema_profile_id") or upstream.get("schema_profile_id"),
            source_context=source_context,
            filter_reason=combined_domain_filter_reason or self._string_or_none(llm_payload.get("filter_reason")),
            domain_filter_applied=domain_filter_applied,
            domain_filter_reason=combined_domain_filter_reason,
            token_trim=token_trim,
            events=(event,),
        )

    def _success_result(
        self,
        request: CapabilityExecutionRequest,
        *,
        columns: list[str],
        rows: list[dict[str, Any]],
        kept_row_indexes: list[int],
        filter_source: str,
        fallback_used: bool,
        fallback_reason: str | None,
        source_row_count: int,
        source_preview_row_count: int,
        candidate_row_count: int,
        truncated: bool,
        route_id: Any = None,
        schema_profile_id: Any = None,
        source_context: Mapping[str, Any] | None = None,
        filter_reason: str | None = None,
        domain_filter_applied: bool = False,
        domain_filter_reason: str | None = None,
        token_trim: _TokenTrimResult | None = None,
        events=(),
    ) -> CapabilityExecutionResult:
        filtered_row_count = len(rows)
        source_payload = self._source_payload(
            rows=rows,
            source_context=source_context or {},
            route_id=route_id,
            filtered_row_count=filtered_row_count,
            source_row_count=source_row_count,
            filter_reason=filter_reason,
        )
        token_trim_payload = self._token_trim_payload(
            token_trim or self._no_token_trim(rows),
            fallback_row_count=source_preview_row_count,
        )
        output = {
            "columns": columns,
            "rows": rows,
            "row_count": filtered_row_count,
            "preview_row_count": filtered_row_count,
            "truncated": truncated,
            "source_row_count": source_row_count,
            "source_preview_row_count": source_preview_row_count,
            "candidate_row_count": candidate_row_count,
            "kept_row_indexes": kept_row_indexes,
            "removed_row_count": max(source_row_count - filtered_row_count, 0),
            "filter_source": filter_source,
            "filter_reason": filter_reason,
            "domain_filter_applied": domain_filter_applied,
            "domain_filter_reason": domain_filter_reason,
            "fallback_used": fallback_used,
            "fallback_reason": fallback_reason,
            "route_id": route_id,
            "schema_profile_id": schema_profile_id,
            **source_payload,
            "summary": source_payload["summary"],
            "satisfaction": self._satisfaction_payload(
                filtered_row_count=filtered_row_count,
                source_row_count=source_row_count,
                filter_reason=filter_reason,
            ),
        }
        output.update(token_trim_payload)
        artifact = make_artifact(
            name="filtered_query_result",
            task_id=request.task_id,
            node_id=request.node_id,
            payload=output,
            summary=f"filtered query result with {filtered_row_count} rows",
        )
        return CapabilityExecutionResult(
            capability_id=request.capability_id,
            task_id=request.task_id,
            node_id=request.node_id,
            output_payload=output,
            artifacts=(artifact,),
            events=tuple(events),
        )

    @staticmethod
    def _satisfaction_payload(
        *,
        filtered_row_count: int,
        source_row_count: int,
        filter_reason: str | None,
    ) -> dict[str, Any]:
        if filtered_row_count > 0:
            return {
                "satisfied": True,
                "reason_code": "matched_rows_found",
                "message": filter_reason or "筛选后存在符合当前查询条件的结果行。",
                "replan_recommended": False,
            }
        if source_row_count <= 0:
            return {
                "satisfied": False,
                "reason_code": "empty_result",
                "message": filter_reason or "SQL 查询未返回候选行。",
                "replan_recommended": True,
            }
        return {
            "satisfied": False,
            "reason_code": "no_relevant_rows_after_filtering",
            "message": filter_reason or "候选行经筛选后没有符合用户需求的结果。",
            "replan_recommended": True,
        }

    @classmethod
    def _source_payload(
        cls,
        *,
        rows: list[dict[str, Any]],
        source_context: Mapping[str, Any],
        route_id: Any,
        filtered_row_count: int,
        source_row_count: int,
        filter_reason: str | None,
    ) -> dict[str, Any]:
        row_tables = cls._source_values_from_rows(rows, keys={"source_table", "来源表"})
        context_tables = cls._string_list(source_context.get("tables_used")) or cls._string_list(source_context.get("selected_tables"))
        tables_used = row_tables or context_tables
        source_scope = source_context.get("source_scope") if isinstance(source_context.get("source_scope"), Mapping) else {}
        row_crops = cls._source_values_from_rows(rows, keys={"source_crop", "crop_name", "作物", "作物名称"})
        context_crops = cls._string_list((source_scope or {}).get("approval_crops"))
        source_crops = row_crops or context_crops
        table_label = cls._table_label(tables_used)
        crop_label = "、".join(cls._crop_display(crop) for crop in source_crops) if source_crops else ""
        match_summary = source_context.get("match_summary") if isinstance(source_context.get("match_summary"), Mapping) else {}
        row_matched_fields = cls._source_values_from_rows(rows, keys={"matched_field", "命中字段"})
        context_matched_fields = cls._string_list(source_context.get("matched_fields")) or cls._fields_from_match_summary(match_summary)
        matched_fields = cls._merge_string_lists(row_matched_fields, context_matched_fields)
        row_match_tiers = cls._source_values_from_rows(rows, keys={"match_tier", "命中等级"})
        context_match_tiers = cls._string_list((source_scope or {}).get("match_tiers")) or cls._tiers_from_match_summary(match_summary)
        match_tiers = cls._merge_string_lists(row_match_tiers, context_match_tiers)
        match_field_summary = cls._match_field_summary(
            matched_fields=matched_fields,
            match_tiers=match_tiers,
            match_summary=match_summary,
        )
        if table_label and crop_label:
            source_summary = f"数据来源：{table_label}（{crop_label}）。"
        elif table_label:
            source_summary = f"数据来源：{table_label}。"
        else:
            source_summary = "数据来源：当前 SQLQuery 已解析的数据表范围。"
        if match_field_summary:
            source_summary = source_summary.rstrip("。") + f"；{match_field_summary}。"
        query_constraints = source_context.get("query_constraints") if isinstance(source_context.get("query_constraints"), Mapping) else {}
        constraint_coverage_summary = (
            source_context.get("constraint_coverage_summary")
            if isinstance(source_context.get("constraint_coverage_summary"), Mapping)
            else {}
        )
        constraint_summary = str((query_constraints or {}).get("constraint_summary") or (constraint_coverage_summary or {}).get("summary") or "").strip()
        if constraint_summary:
            source_summary = source_summary.rstrip("。") + f"；查询约束：{constraint_summary}。"

        no_result_explanation = None
        if filtered_row_count <= 0:
            search_effort = str(source_context.get("search_effort_summary") or "").strip()
            if source_row_count <= 0:
                no_result_explanation = (
                    f"{search_effort}，但没有返回匹配记录。"
                    if search_effort
                    else f"已在{table_label or '当前解析的数据表范围'}中执行查询，但没有返回匹配记录。"
                )
            else:
                effort = filter_reason or "系统已对 SQL 返回的候选行做相关性筛选"
                no_result_explanation = f"已在{table_label or '当前解析的数据表范围'}中查询并获得候选结果；{effort}，最终没有保留匹配记录。"

        if filtered_row_count > 0:
            summary = f"查询已完成，共返回 {filtered_row_count} 行结果；{source_summary}"
        else:
            summary = f"查询已完成，但没有返回匹配结果；{no_result_explanation or source_summary}"

        return {
            "tables_used": tables_used,
            "selected_tables": cls._string_list(source_context.get("selected_tables")),
            "source_scope": {
                **dict(source_scope or {}),
                "tables_used": tables_used,
                "approval_crops": source_crops,
                "matched_fields": matched_fields,
                "match_tiers": match_tiers,
            },
            "match_summary": dict(match_summary or {}),
            "matched_fields": matched_fields,
            "match_tiers": match_tiers,
            "search_effort_summary": source_context.get("search_effort_summary"),
            "query_constraints": dict(query_constraints or {}),
            "constraint_coverage_summary": dict(constraint_coverage_summary or {}),
            "source_summary": source_summary,
            "no_result_explanation": no_result_explanation,
            "summary": summary,
        }

    @staticmethod
    def _source_values_from_rows(rows: list[dict[str, Any]], *, keys: set[str]) -> list[str]:
        values: list[str] = []
        normalized_keys = {key.lower() for key in keys}
        for row in rows:
            for raw_key, raw_value in row.items():
                key = str(raw_key or "").strip().strip("`").lower()
                suffix = key.rsplit(".", 1)[-1]
                if key not in normalized_keys and suffix not in normalized_keys:
                    continue
                value = str(raw_value or "").strip()
                if value and value not in values:
                    values.append(value)
        return values

    @staticmethod
    def _string_list(value: Any) -> list[str]:
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list | tuple):
            return []
        result: list[str] = []
        for item in value:
            text = str(item or "").strip()
            if text and text not in result:
                result.append(text)
        return result

    @staticmethod
    def _merge_string_lists(*values: list[str]) -> list[str]:
        result: list[str] = []
        for items in values:
            for item in items:
                text = str(item or "").strip()
                if text and text not in result:
                    result.append(text)
        return result

    @classmethod
    def _table_label(cls, tables: list[str]) -> str:
        return "、".join(f"`{table}`" for table in tables)

    @staticmethod
    def _crop_display(crop: str) -> str:
        return {
            "corn": "玉米",
            "rice": "水稻",
            "cotton": "棉花",
            "wheat": "小麦",
            "soybean": "大豆",
            "玉米": "玉米",
            "水稻": "水稻",
            "棉花": "棉花",
            "小麦": "小麦",
            "大豆": "大豆",
        }.get(str(crop), str(crop))

    @staticmethod
    def _fields_from_match_summary(match_summary: Any) -> list[str]:
        if not isinstance(match_summary, Mapping):
            return []
        result: list[str] = []
        for tier in ("primary", "secondary", "peer"):
            for item in list(match_summary.get(tier) or []):
                if not isinstance(item, Mapping):
                    continue
                field = str(item.get("field") or "").strip()
                if field and field not in result:
                    result.append(field)
        return result

    @staticmethod
    def _tiers_from_match_summary(match_summary: Any) -> list[str]:
        if not isinstance(match_summary, Mapping):
            return []
        return [tier for tier in ("primary", "secondary", "peer") if match_summary.get(tier)]

    @classmethod
    def _match_field_summary(
        cls,
        *,
        matched_fields: list[str],
        match_tiers: list[str],
        match_summary: Any,
    ) -> str:
        if isinstance(match_summary, Mapping) and any(match_summary.get(tier) for tier in ("primary", "secondary", "peer")):
            parts: list[str] = []
            labels = {"primary": "主要命中", "secondary": "附带命中", "peer": "同级命中"}
            for tier in ("primary", "secondary", "peer"):
                fields = []
                for item in list(match_summary.get(tier) or []):
                    if not isinstance(item, Mapping):
                        continue
                    field = str(item.get("field") or "").strip()
                    if field and field not in fields:
                        fields.append(field)
                if fields:
                    parts.append(f"{labels[tier]}字段：{'、'.join(cls._field_display(field) for field in fields)}")
            if parts:
                return "；".join(parts)
        if matched_fields:
            suffix = ""
            if match_tiers:
                suffix = f"（{ '、'.join(match_tiers) }）"
            return f"命中字段：{'、'.join(cls._field_display(field) for field in matched_fields)}{suffix}"
        return ""

    @staticmethod
    def _field_display(field: str) -> str:
        return {
            "variety_name": "品种名",
            "applicant": "申请者",
            "breeder": "育种者",
            "approval_num": "审定编号",
        }.get(str(field), str(field))

    def _fallback_result(
        self,
        request: CapabilityExecutionRequest,
        *,
        columns: list[str],
        rows: list[dict[str, Any]],
        kept_row_indexes: list[int],
        fallback_reason: str,
        diagnostic: str | None,
        source_row_count: int,
        source_preview_row_count: int,
        candidate_row_count: int,
        truncated: bool,
        route_id: Any = None,
        schema_profile_id: Any = None,
        source_context: Mapping[str, Any] | None = None,
        domain_filter_applied: bool = False,
        domain_filter_reason: str | None = None,
        token_trim: _TokenTrimResult | None = None,
    ) -> CapabilityExecutionResult:
        event_payload = {
            "capability_id": self.capability_id, "stage": "result_filtering",
            "fallback_reason": fallback_reason,
            "prompt_recorded": False,
            "rows_recorded": "candidate_only",
            "source_row_count": source_row_count,
            "candidate_row_count": candidate_row_count,
            "filtered_row_count": len(rows),
            "truncated": truncated,
        }
        if token_trim is not None:
            event_payload["token_trim_applied"] = token_trim.applied
            event_payload["token_trim_removed_row_count"] = token_trim.removed_row_count
        if diagnostic:
            event_payload["diagnostic"] = diagnostic[:300]
        event = make_audit_event(request, event_type=SQL_QUERY_AUDIT_LLM_FALLBACK_EVENT, payload=event_payload)
        return self._success_result(
            request,
            columns=columns,
            rows=rows,
            kept_row_indexes=kept_row_indexes,
            filter_source="fallback",
            fallback_used=True,
            fallback_reason=fallback_reason,
            source_row_count=source_row_count,
            source_preview_row_count=source_preview_row_count,
            candidate_row_count=candidate_row_count,
            truncated=truncated,
            route_id=route_id,
            schema_profile_id=schema_profile_id,
            source_context=source_context,
            filter_reason=domain_filter_reason or "LLM 筛选失败，保守保留 SQL 查询返回的候选表格。",
            domain_filter_applied=domain_filter_applied,
            domain_filter_reason=domain_filter_reason,
            token_trim=token_trim,
            events=(event,),
        )

    def _trim_rows_for_llm(self, rows: list[dict[str, Any]]) -> _TokenTrimResult:
        if self._trim_max_tokens is None:
            return self._no_token_trim(rows)

        full_token_num = 0
        trimmed_token_num = 0
        trimmed_rows: list[dict[str, Any]] = []
        for row in reversed(rows):
            row_token_num = self._row_token_num(row)
            full_token_num += row_token_num
            if full_token_num <= self._trim_max_tokens:
                trimmed_rows.append(row)
                trimmed_token_num = full_token_num
                continue
            return _TokenTrimResult(
                rows=trimmed_rows,
                applied=True,
                trim_max_tokens=self._trim_max_tokens,
                full_token_num=full_token_num,
                trimmed_token_num=trimmed_token_num,
                removed_row_count=len(rows) - len(trimmed_rows),
            )

        return _TokenTrimResult(
            rows=rows,
            applied=False,
            trim_max_tokens=self._trim_max_tokens,
            full_token_num=full_token_num,
            trimmed_token_num=trimmed_token_num,
            removed_row_count=0,
        )

    @staticmethod
    def _no_token_trim(rows: list[dict[str, Any]]) -> _TokenTrimResult:
        return _TokenTrimResult(
            rows=rows,
            applied=False,
            trim_max_tokens=None,
            full_token_num=None,
            trimmed_token_num=None,
            removed_row_count=0,
        )

    @staticmethod
    def _row_token_num(row: Mapping[str, Any]) -> int:
        row_text = json.dumps(json_ready(dict(row)), ensure_ascii=False, sort_keys=True, default=str)
        return get_num_of_tokens_from_text(row_text)

    @staticmethod
    def _token_trim_payload(
        token_trim: _TokenTrimResult,
        *,
        fallback_row_count: int,
    ) -> dict[str, Any]:
        return {
            "token_trim_applied": token_trim.applied,
            "token_trim_max_tokens": token_trim.trim_max_tokens,
            "token_trim_full_token_num": token_trim.full_token_num,
            "token_trimmed_token_num": token_trim.trimmed_token_num,
            "token_trimmed_row_count": len(token_trim.rows) if token_trim.trim_max_tokens is not None else fallback_row_count,
            "token_trim_removed_row_count": token_trim.removed_row_count,
        }

    def _parse_keep_row_indexes(self, payload: Mapping[str, Any], *, candidate_row_count: int) -> list[int]:
        raw_indexes = payload.get("keep_row_indexes")
        if raw_indexes is None:
            raw_indexes = payload.get("kept_row_indexes")
        if not isinstance(raw_indexes, list):
            raise LLMOutputError("validation_failed", "LLM filtering output requires keep_row_indexes list.")

        seen: set[int] = set()
        kept: list[int] = []
        for raw_index in raw_indexes:
            if isinstance(raw_index, bool) or not isinstance(raw_index, int):
                raise LLMOutputError("validation_failed", "keep_row_indexes must contain integer row indexes.")
            if raw_index < 0 or raw_index >= candidate_row_count:
                raise LLMOutputError("validation_failed", "keep_row_indexes contains an out-of-range row index.")
            if raw_index not in seen:
                kept.append(raw_index)
                seen.add(raw_index)
        return kept

    def _specific_variety_keep_indexes(
        self,
        *,
        rows: list[dict[str, Any]],
        upstream: Mapping[str, Any],
        question_context: Mapping[str, Any],
    ) -> tuple[list[int] | None, str | None]:
        targets = self._specific_variety_targets(upstream, question_context)
        if not targets:
            return None, None

        kept = [
            index
            for index, row in enumerate(rows)
            if self._row_matches_specific_variety(row, targets=targets)
        ]
        if not kept or len(kept) == len(rows):
            return None, None

        target_label = " / ".join(sorted(targets))
        return kept, f"用户查询的是明确品种编号 {target_label}；仅保留品种名规范化后等于该编号或该编号+“号”的候选行。"

    def _specific_variety_targets(
        self,
        upstream: Mapping[str, Any],
        question_context: Mapping[str, Any],
    ) -> set[str]:
        sql_candidates = [upstream.get("sql"), question_context.get("sql")]
        targets: set[str] = set()
        for sql in sql_candidates:
            if not isinstance(sql, str):
                continue
            for match in _VARIETY_LIKE_PATTERN.finditer(sql):
                normalized = self._normalize_variety_token(match.group(1))
                if normalized and any(char.isdigit() for char in normalized):
                    targets.add(normalized)
        return targets

    def _organization_like_protected_indexes(
        self,
        *,
        rows: list[dict[str, Any]],
        upstream: Mapping[str, Any],
        question_context: Mapping[str, Any],
    ) -> tuple[list[int] | None, str | None]:
        targets = self._organization_like_targets(upstream, question_context)
        if not targets:
            return None, None

        protected = [
            index
            for index, row in enumerate(rows)
            if self._row_matches_organization_like(row, targets=targets)
        ]
        if not protected:
            return None, None

        target_label = " / ".join(sorted(targets))
        return protected, f"用户查询企业简称或主体关键词 {target_label}；保留申请者/育种者等企业字段中包含该简称、完整名称、子公司或关联主体名称的候选行。"

    def _organization_like_targets(
        self,
        upstream: Mapping[str, Any],
        question_context: Mapping[str, Any],
    ) -> set[str]:
        sql_candidates = [upstream.get("sql"), question_context.get("sql")]
        targets: set[str] = set()
        for sql in sql_candidates:
            if not isinstance(sql, str):
                continue
            for match in _ORGANIZATION_LIKE_PATTERN.finditer(sql):
                normalized = self._normalize_entity_token(match.group(1))
                if normalized:
                    targets.add(normalized)
        return targets

    @classmethod
    def _row_matches_organization_like(cls, row: Mapping[str, Any], *, targets: set[str]) -> bool:
        for key, value in row.items():
            if not cls._is_organization_key(key):
                continue
            normalized = cls._normalize_entity_token(value)
            if any(target in normalized for target in targets):
                return True
        return False

    @classmethod
    def _row_matches_specific_variety(cls, row: Mapping[str, Any], *, targets: set[str]) -> bool:
        for key, value in row.items():
            if not cls._is_variety_name_key(key):
                continue
            normalized = cls._normalize_variety_token(value)
            if normalized in targets:
                return True
        return False

    @staticmethod
    def _is_variety_name_key(key: Any) -> bool:
        normalized = str(key or "").strip().strip("`").lower()
        return normalized == "variety_name" or normalized.endswith(".variety_name")

    @staticmethod
    def _is_organization_key(key: Any) -> bool:
        normalized = str(key or "").strip().strip("`").lower()
        if normalized in _ORGANIZATION_ROW_KEYS:
            return True
        suffix = normalized.rsplit(".", 1)[-1]
        return suffix in _ORGANIZATION_ROW_KEYS

    @staticmethod
    def _normalize_variety_token(value: Any) -> str:
        text = unicodedata.normalize("NFKC", str(value or "")).strip().lower()
        text = re.sub(r"\s+", "", text)
        text = text.rstrip("号")
        return text

    @staticmethod
    def _normalize_entity_token(value: Any) -> str:
        text = unicodedata.normalize("NFKC", str(value or "")).strip().lower()
        return re.sub(r"\s+", "", text)

    @staticmethod
    def _apply_domain_keep_indexes(
        llm_indexes: list[int],
        *,
        domain_keep_indexes: list[int] | None,
    ) -> list[int]:
        if domain_keep_indexes is None:
            return llm_indexes
        domain_set = set(domain_keep_indexes)
        intersected = [index for index in llm_indexes if index in domain_set]
        return intersected or list(domain_keep_indexes)

    @staticmethod
    def _apply_protected_keep_indexes(
        llm_indexes: list[int],
        *,
        protected_keep_indexes: list[int] | None,
    ) -> list[int]:
        if protected_keep_indexes is None:
            return llm_indexes
        merged: list[int] = []
        seen: set[int] = set()
        for index in [*llm_indexes, *protected_keep_indexes]:
            if index in seen:
                continue
            merged.append(index)
            seen.add(index)
        return merged

    @staticmethod
    def _join_filter_reasons(*reasons: str | None) -> str | None:
        joined = "；".join(reason for reason in reasons if reason)
        return joined or None

    def _find_optional_dependency_output(
        self,
        request: CapabilityExecutionRequest,
        required_keys: tuple[str, ...],
    ) -> dict[str, Any]:
        try:
            return find_dependency_output(request, required_keys)
        except ValueError:
            return {}

    @staticmethod
    def _normalize_rows(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list | tuple):
            return []
        normalized: list[dict[str, Any]] = []
        for row in value:
            ready = json_ready(dict(row) if isinstance(row, Mapping) else row)
            if isinstance(ready, dict):
                normalized.append(ready)
        return normalized

    @staticmethod
    def _int_or_default(value: Any, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _string_or_none(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        stripped = value.strip()
        return stripped or None
