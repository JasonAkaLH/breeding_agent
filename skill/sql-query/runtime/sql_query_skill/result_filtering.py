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
        domain_filter_applied = domain_keep_indexes is not None

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
                filter_reason=domain_filter_reason or ("未配置 LLM 筛选器，保留 SQL 查询返回的候选表格。" if source_row_count else "查询未返回候选行。"),
                domain_filter_applied=domain_filter_applied,
                domain_filter_reason=domain_filter_reason,
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
                domain_filter_applied=domain_filter_applied,
                domain_filter_reason=domain_filter_reason,
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
                domain_filter_applied=domain_filter_applied,
                domain_filter_reason=domain_filter_reason,
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
            filter_reason=domain_filter_reason or self._string_or_none(llm_payload.get("filter_reason")),
            domain_filter_applied=domain_filter_applied,
            domain_filter_reason=domain_filter_reason,
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
        filter_reason: str | None = None,
        domain_filter_applied: bool = False,
        domain_filter_reason: str | None = None,
        token_trim: _TokenTrimResult | None = None,
        events=(),
    ) -> CapabilityExecutionResult:
        filtered_row_count = len(rows)
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
    def _normalize_variety_token(value: Any) -> str:
        text = unicodedata.normalize("NFKC", str(value or "")).strip().lower()
        text = re.sub(r"\s+", "", text)
        text = text.rstrip("号")
        return text

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
