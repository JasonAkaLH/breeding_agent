from __future__ import annotations

from collections.abc import Callable
from typing import Any

from src.core.contracts import CapabilityContract, CapabilityExecutionRequest, CapabilityExecutionResult
from src.core.enums import ArtifactType

from .helpers import find_dependency_output, make_artifact, make_audit_event
from .llm_utils import LLMOutputError, TextGenerator, call_text_generator, parse_json_object, string_list
from .prompt_builders import (
    DEFAULT_MAX_SUMMARY_CHARS,
    DEFAULT_SUMMARY_PREVIEW_ROWS,
    build_result_summary_prompt,
    build_result_summary_prompt_payload,
)


class SQLQueryResultSummarizeCapability(CapabilityContract):
    capability_id = "sql_query.result_summarize"
    version = "1"
    description = "Summarize readonly SQL results for user-facing output with a deterministic fallback path."

    def __init__(
        self,
        *,
        summarizer: Callable[[dict[str, Any]], str] | None = None,
        llm_text_generator: TextGenerator | None = None,
        max_preview_rows: int = DEFAULT_SUMMARY_PREVIEW_ROWS,
        max_summary_chars: int = DEFAULT_MAX_SUMMARY_CHARS,
    ) -> None:
        self._summarizer = summarizer or self._default_summarizer
        self._llm_text_generator = llm_text_generator
        self._max_preview_rows = max_preview_rows
        self._max_summary_chars = max_summary_chars

    async def execute(self, request: CapabilityExecutionRequest) -> CapabilityExecutionResult:
        upstream = find_dependency_output(request, ("rows", "columns", "row_count"))
        question_context = self._find_optional_dependency_output(
            request,
            ("user_question", "route_id", "schema_profile_id"),
        )
        prompt_payload = build_result_summary_prompt_payload(
            upstream,
            question_context=question_context,
            max_preview_rows=self._max_preview_rows,
            max_summary_chars=self._max_summary_chars,
        )
        row_count = int(prompt_payload["result_context"]["row_count"])
        preview_row_count = int(prompt_payload["result_context"]["preview_row_count"])
        truncated = bool(prompt_payload["result_context"]["truncated"])

        if row_count == 0:
            summary = self._default_summarizer(upstream)
            return self._success_result(
                request,
                summary=summary,
                summary_source="deterministic",
                fallback_used=False,
                fallback_reason=None,
                row_count=row_count,
                preview_row_count=preview_row_count,
                truncated=truncated,
                route_id=question_context.get("route_id"),
                schema_profile_id=question_context.get("schema_profile_id"),
                events=(),
            )

        if self._llm_text_generator is None:
            try:
                summary = self._summarizer(upstream)
                return self._success_result(
                    request,
                    summary=summary,
                    summary_source="deterministic",
                    fallback_used=False,
                    fallback_reason=None,
                    row_count=row_count,
                    preview_row_count=preview_row_count,
                    truncated=truncated,
                    route_id=question_context.get("route_id"),
                    schema_profile_id=question_context.get("schema_profile_id"),
                    events=(),
                )
            except Exception as exc:
                return self._fallback_result(
                    request,
                    upstream,
                    fallback_reason="summarizer_failed",
                    diagnostic=str(exc),
                    row_count=row_count,
                    preview_row_count=preview_row_count,
                    truncated=truncated,
                    route_id=question_context.get("route_id"),
                    schema_profile_id=question_context.get("schema_profile_id"),
                )

        prompt = build_result_summary_prompt(
            upstream,
            question_context=question_context,
            max_preview_rows=self._max_preview_rows,
            max_summary_chars=self._max_summary_chars,
        )
        try:
            raw_output = await call_text_generator(self._llm_text_generator, prompt)
            llm_payload = parse_json_object(raw_output)
            summary = str(llm_payload.get("summary") or "").strip()
            if not summary:
                raise LLMOutputError("validation_failed", "LLM summary requires non-empty summary.")
            if len(summary) > self._max_summary_chars:
                raise LLMOutputError("summary_too_long", "LLM summary exceeds max_summary_chars.")
        except LLMOutputError as exc:
            return self._fallback_result(
                request,
                upstream,
                fallback_reason=exc.reason,
                diagnostic=str(exc),
                row_count=row_count,
                preview_row_count=preview_row_count,
                truncated=truncated,
                route_id=question_context.get("route_id"),
                schema_profile_id=question_context.get("schema_profile_id"),
            )
        except Exception as exc:
            return self._fallback_result(
                request,
                upstream,
                fallback_reason="provider_failed",
                diagnostic=str(exc),
                row_count=row_count,
                preview_row_count=preview_row_count,
                truncated=truncated,
                route_id=question_context.get("route_id"),
                schema_profile_id=question_context.get("schema_profile_id"),
            )

        event = make_audit_event(
            request,
            event_type="sql_query.llm_call",
            payload={
                "node_name": self.capability_id,
                "status": "succeeded",
                "summary_source": "llm",
                "fallback_used": False,
                "prompt_recorded": False,
                "rows_recorded": "preview_only",
                "row_count": row_count,
                "preview_row_count": preview_row_count,
                "truncated": truncated,
            },
        )
        output_extras = {
            "highlights": string_list(llm_payload.get("highlights")),
            "caveats": string_list(llm_payload.get("caveats")),
        }
        return self._success_result(
            request,
            summary=summary,
            summary_source="llm",
            fallback_used=False,
            fallback_reason=None,
            row_count=row_count,
            preview_row_count=preview_row_count,
            truncated=truncated,
            route_id=question_context.get("route_id"),
            schema_profile_id=question_context.get("schema_profile_id"),
            events=(event,),
            output_extras=output_extras,
        )

    def _success_result(
        self,
        request: CapabilityExecutionRequest,
        *,
        summary: str,
        summary_source: str,
        fallback_used: bool,
        fallback_reason: str | None,
        row_count: int,
        preview_row_count: int,
        truncated: bool,
        route_id: Any = None,
        schema_profile_id: Any = None,
        events=(),
        output_extras: dict[str, Any] | None = None,
    ) -> CapabilityExecutionResult:
        output = {
            "summary": summary,
            "summary_source": summary_source,
            "fallback_used": fallback_used,
            "fallback_reason": fallback_reason,
            "row_count": row_count,
            "preview_row_count": preview_row_count,
            "truncated": truncated,
            "route_id": route_id,
            "schema_profile_id": schema_profile_id,
        }
        if output_extras:
            output.update(output_extras)
        artifact = make_artifact(
            name="result_summary",
            task_id=request.task_id,
            node_id=request.node_id,
            payload=output,
            summary=summary,
            artifact_type=ArtifactType.SUMMARY,
        )
        return CapabilityExecutionResult(
            capability_id=request.capability_id,
            task_id=request.task_id,
            node_id=request.node_id,
            output_payload=output,
            artifacts=(artifact,),
            events=tuple(events),
        )

    def _fallback_result(
        self,
        request: CapabilityExecutionRequest,
        upstream: dict[str, Any],
        *,
        fallback_reason: str,
        diagnostic: str | None,
        row_count: int,
        preview_row_count: int,
        truncated: bool,
        route_id: Any = None,
        schema_profile_id: Any = None,
    ) -> CapabilityExecutionResult:
        summary = self._fallback_summary(upstream)
        event_payload = {
            "node_name": self.capability_id,
            "fallback_reason": fallback_reason,
            "prompt_recorded": False,
            "rows_recorded": "preview_only",
            "row_count": row_count,
            "preview_row_count": preview_row_count,
            "truncated": truncated,
        }
        if diagnostic:
            event_payload["diagnostic"] = diagnostic[:300]
        event = make_audit_event(request, event_type="sql_query.llm_fallback", payload=event_payload)
        return self._success_result(
            request,
            summary=summary,
            summary_source="fallback",
            fallback_used=True,
            fallback_reason=fallback_reason,
            row_count=row_count,
            preview_row_count=preview_row_count,
            truncated=truncated,
            route_id=route_id,
            schema_profile_id=schema_profile_id,
            events=(event,),
        )

    def _default_summarizer(self, upstream: dict[str, Any]) -> str:
        row_count = int(upstream["row_count"])
        columns = list(upstream["columns"])
        rows = list(upstream["rows"])
        if row_count == 0:
            return "查询完成，共返回 0 行结果。"
        preview = ", ".join(f"{key}={value}" for key, value in rows[0].items())
        return f"查询完成，共返回 {row_count} 行结果；列为 {', '.join(columns)}；首行预览：{preview}。"

    def _fallback_summary(self, upstream: dict[str, Any]) -> str:
        row_count = int(upstream["row_count"])
        columns = list(upstream["columns"])
        return f"结果摘要降级输出：row_count={row_count}; columns={columns!r}"

    def _find_optional_dependency_output(
        self,
        request: CapabilityExecutionRequest,
        required_keys: tuple[str, ...],
    ) -> dict[str, Any]:
        try:
            return find_dependency_output(request, required_keys)
        except ValueError:
            return {}
