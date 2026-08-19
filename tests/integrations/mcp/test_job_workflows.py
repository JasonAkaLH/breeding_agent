from __future__ import annotations

import asyncio
import unittest

from src.integrations.mcp.client import MCPRemoteError
from src.integrations.mcp.job_workflows import (
    MCPJobWorkflowError,
    extract_ocr_text_projection,
    run_ocr_async_job_workflow,
)


def _result(content, *, is_error: bool = False):
    return {
        "content": [{"type": "text", "text": "safe"}],
        "structuredContent": content,
        "isError": is_error,
    }


class _Adapter:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    async def call_tool(self, name, arguments, **kwargs):
        self.calls.append((name, dict(arguments), dict(kwargs)))
        value = self.results.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


class OCRAsyncJobWorkflowTests(unittest.IsolatedAsyncioTestCase):
    def test_extracts_bounded_markdown_projection(self) -> None:
        self.assertEqual(
            extract_ocr_text_projection(
                _result({"status": "succeeded", "markdown": "  # OCR\n识别成功  "})
            ),
            "# OCR\n识别成功",
        )
        self.assertIsNone(
            extract_ocr_text_projection(_result({"status": "succeeded"}))
        )

    async def test_runs_start_poll_success_and_ack(self) -> None:
        final = _result(
            {
                "job_id": "job-1",
                "status": "succeeded",
                "markdown": "# OCR\n\n识别成功",
                "result": {"pages": [{"text": "识别成功"}]},
                "result_receipt": "receipt-1",
            }
        )
        adapter = _Adapter(
            _result({"job_id": "job-1", "status": "queued"}),
            _result({"job_id": "job-1", "status": "running"}),
            final,
            _result({"job_id": "job-1", "status": "acknowledged"}),
        )
        sleeps = []

        async def sleep(seconds):
            sleeps.append(seconds)

        result = await run_ocr_async_job_workflow(
            adapter,
            {"source": {"type": "base64", "data": "AA==", "mime_type": "image/png"}},
            sleep=sleep,
            timeout_seconds=30,
            poll_interval_seconds=2,
        )

        self.assertEqual(result, final)
        self.assertEqual(
            [call[0] for call in adapter.calls],
            ["start_parse_job", "get_parse_job", "get_parse_job", "ack_parse_job"],
        )
        self.assertEqual(sleeps, [2])
        self.assertEqual(adapter.calls[1][1]["include_result"], True)

    async def test_persists_final_result_before_ack(self) -> None:
        order = []

        class OrderedAdapter(_Adapter):
            async def call_tool(self, name, arguments, **kwargs):
                if name == "ack_parse_job":
                    order.append("ack")
                return await super().call_tool(name, arguments, **kwargs)

        final = _result(
            {
                "job_id": "job-1",
                "status": "succeeded",
                "markdown": "识别成功",
                "result_receipt": "receipt-1",
            }
        )
        adapter = OrderedAdapter(
            _result({"job_id": "job-1", "status": "queued"}),
            final,
            _result({"job_id": "job-1", "status": "acknowledged"}),
        )

        async def persist(result):
            self.assertEqual(result, final)
            order.append("persist")
            return "durable-result"

        result = await run_ocr_async_job_workflow(
            adapter,
            {"source": {}},
            result_persisted_callback=persist,
        )

        self.assertEqual(result, "durable-result")
        self.assertEqual(order, ["persist", "ack"])

    async def test_tool_is_error_is_terminal_remote_error(self) -> None:
        adapter = _Adapter(
            _result(
                {
                    "error": {
                        "code": "INVALID_ARGUMENT",
                        "message": "Unsupported source type: None",
                    }
                },
                is_error=True,
            )
        )
        with self.assertRaisesRegex(MCPRemoteError, "isError=true"):
            await run_ocr_async_job_workflow(adapter, {"source": {}})
        self.assertEqual(len(adapter.calls), 1)

    async def test_terminal_job_failure_does_not_ack(self) -> None:
        adapter = _Adapter(
            _result({"job_id": "job-1", "status": "queued"}),
            _result(
                {
                    "job_id": "job-1",
                    "status": "failed",
                    "error": {"code": "OCR_INFERENCE_FAILED"},
                }
            ),
        )
        with self.assertRaisesRegex(MCPJobWorkflowError, "mcp_ocr_job_failed"):
            await run_ocr_async_job_workflow(
                adapter,
                {"source": {}},
                sleep=lambda _seconds: asyncio.sleep(0),
            )
        self.assertEqual([call[0] for call in adapter.calls], ["start_parse_job", "get_parse_job"])

    async def test_cancellation_attempts_remote_job_cancel(self) -> None:
        entered = asyncio.Event()

        class _CancellingAdapter(_Adapter):
            async def call_tool(self, name, arguments, **kwargs):
                if name == "start_parse_job":
                    self.calls.append((name, dict(arguments), dict(kwargs)))
                    return _result({"job_id": "job-1", "status": "queued"})
                if name == "get_parse_job":
                    self.calls.append((name, dict(arguments), dict(kwargs)))
                    entered.set()
                    await asyncio.Future()
                return await super().call_tool(name, arguments, **kwargs)

        adapter = _CancellingAdapter(_result({"job_id": "job-1", "status": "cancelled"}))
        task = asyncio.create_task(run_ocr_async_job_workflow(adapter, {"source": {}}))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(adapter.calls[-1][0], "cancel_parse_job")
