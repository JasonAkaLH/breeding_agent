from __future__ import annotations

from collections.abc import Callable
from typing import Any

from src.core.contracts import CapabilityContract, CapabilityExecutionRequest, CapabilityExecutionResult
from src.core.enums import ArtifactType

from .helpers import find_dependency_output, make_artifact


class NL2SQLResultSummarizeCapability(CapabilityContract):
    capability_id = "nl2sql.result_summarize"
    version = "1"
    description = "Summarize readonly SQL results for user-facing output with a deterministic fallback path."

    def __init__(self, *, summarizer: Callable[[dict[str, Any]], str] | None = None) -> None:
        self._summarizer = summarizer or self._default_summarizer

    async def execute(self, request: CapabilityExecutionRequest) -> CapabilityExecutionResult:
        upstream = find_dependency_output(request, ("rows", "columns", "row_count"))
        fallback_used = False
        try:
            summary = self._summarizer(upstream)
        except Exception:
            summary = self._fallback_summary(upstream)
            fallback_used = True

        output = {"summary": summary, "fallback_used": fallback_used, "row_count": upstream["row_count"]}
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
