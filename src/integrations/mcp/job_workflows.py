from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from time import monotonic
from typing import Any

from .client import MCPRemoteError


_WORKING_STATUSES = frozenset({"queued", "running", "working", "cancelling"})
_FAILED_STATUSES = frozenset({"failed", "cancelled", "expired", "gone"})


class MCPJobWorkflowError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


async def run_ocr_async_job_workflow(
    adapter: Any,
    start_arguments: Mapping[str, Any],
    *,
    request_registered_callback: Callable[[str | int], None] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    monotonic_fn: Callable[[], float] = monotonic,
    timeout_seconds: float = 3600,
    poll_interval_seconds: float = 2,
    result_persisted_callback: Callable[[Mapping[str, Any]], Awaitable[Any]] | None = None,
) -> Any:
    if timeout_seconds <= 0 or poll_interval_seconds <= 0:
        raise ValueError("OCR workflow timing limits must be positive")
    job_id: str | None = None
    deadline = monotonic_fn() + timeout_seconds
    try:
        started = await _call(
            adapter,
            "start_parse_job",
            dict(start_arguments),
            request_registered_callback=request_registered_callback,
        )
        started_content = _successful_content(started)
        job_id = _non_empty_string(started_content.get("job_id"))
        if job_id is None:
            raise MCPJobWorkflowError("mcp_ocr_job_id_missing")
        while monotonic_fn() < deadline:
            polled = await _call(
                adapter,
                "get_parse_job",
                {
                    "job_id": job_id,
                    "include_result": True,
                    "result_format": "both",
                },
                request_registered_callback=request_registered_callback,
            )
            if _tool_error_code(polled) == "RESULT_NOT_READY":
                await sleep(poll_interval_seconds)
                continue
            content = _successful_content(polled)
            status = str(content.get("status") or "").strip().lower()
            if status in _FAILED_STATUSES:
                raise MCPJobWorkflowError("mcp_ocr_job_failed")
            if status == "succeeded":
                receipt = _non_empty_string(content.get("result_receipt"))
                persisted = (
                    await result_persisted_callback(polled)
                    if result_persisted_callback is not None
                    else polled
                )
                if receipt is not None:
                    await _ack_best_effort(
                        adapter,
                        job_id,
                        receipt,
                        request_registered_callback=request_registered_callback,
                    )
                return persisted
            if status not in _WORKING_STATUSES:
                raise MCPJobWorkflowError("mcp_ocr_job_status_invalid")
            await sleep(poll_interval_seconds)
        raise MCPJobWorkflowError("mcp_ocr_job_timeout")
    except asyncio.CancelledError:
        if job_id is not None:
            await _cancel_best_effort(
                adapter,
                job_id,
                request_registered_callback=request_registered_callback,
            )
        raise


async def _call(
    adapter: Any,
    tool_name: str,
    arguments: Mapping[str, Any],
    *,
    request_registered_callback: Callable[[str | int], None] | None,
) -> Mapping[str, Any]:
    kwargs: dict[str, Any] = {}
    if request_registered_callback is not None:
        kwargs["request_registered_callback"] = request_registered_callback
    result = await adapter.call_tool(tool_name, dict(arguments), **kwargs)
    if not isinstance(result, Mapping):
        raise MCPJobWorkflowError("mcp_ocr_job_result_invalid")
    return dict(result)


def _successful_content(result: Mapping[str, Any]) -> Mapping[str, Any]:
    error_code = _tool_error_code(result)
    if error_code is not None:
        raise MCPRemoteError(
            "MCP tool returned isError=true.",
            remote_code=error_code,
            retriable=error_code in {"RESULT_NOT_READY", "QUEUE_FULL"},
        )
    content = result.get("structuredContent")
    if not isinstance(content, Mapping):
        raise MCPJobWorkflowError("mcp_ocr_job_result_invalid")
    return content


def _tool_error_code(result: Mapping[str, Any]) -> str | None:
    if result.get("isError") is not True:
        return None
    content = result.get("structuredContent")
    error = content.get("error") if isinstance(content, Mapping) else None
    code = error.get("code") if isinstance(error, Mapping) else None
    normalized = str(code or "MCP_TOOL_ERROR").strip().upper()
    return normalized or "MCP_TOOL_ERROR"


async def _ack_best_effort(
    adapter: Any,
    job_id: str,
    receipt: str,
    *,
    request_registered_callback: Callable[[str | int], None] | None,
) -> None:
    try:
        await _call(
            adapter,
            "ack_parse_job",
            {"job_id": job_id, "result_receipt": receipt},
            request_registered_callback=request_registered_callback,
        )
    except Exception:
        return


async def _cancel_best_effort(
    adapter: Any,
    job_id: str,
    *,
    request_registered_callback: Callable[[str | int], None] | None,
) -> None:
    try:
        task = asyncio.create_task(
            _call(
                adapter,
                "cancel_parse_job",
                {"job_id": job_id},
                request_registered_callback=request_registered_callback,
            )
        )
        await asyncio.shield(task)
    except BaseException:
        return


def _non_empty_string(value: object) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


__all__ = ["MCPJobWorkflowError", "run_ocr_async_job_workflow"]
