from __future__ import annotations

import asyncio
import unittest

from src.capabilities.nl2sql.result_summarize import NL2SQLResultSummarizeCapability

from tests.capabilities.nl2sql.support import make_request


class NL2SQLResultSummarizeTest(unittest.TestCase):
    def test_summarizes_query_results(self) -> None:
        capability = NL2SQLResultSummarizeCapability()
        request = make_request(
            "nl2sql.result_summarize",
            dependency_outputs={
                "execute": {
                    "columns": ["variety_name"],
                    "rows": [{"variety_name": "先玉335"}],
                    "row_count": 1,
                }
            },
        )

        result = asyncio.run(capability.execute(request))

        self.assertIn("查询完成", result.output_payload["summary"])
        self.assertFalse(result.output_payload["fallback_used"])

    def test_fallback_summary_when_custom_summarizer_fails(self) -> None:
        def broken(_: dict) -> str:
            raise RuntimeError("boom")

        capability = NL2SQLResultSummarizeCapability(summarizer=broken)
        request = make_request(
            "nl2sql.result_summarize",
            dependency_outputs={
                "execute": {
                    "columns": ["variety_name"],
                    "rows": [{"variety_name": "先玉335"}],
                    "row_count": 1,
                }
            },
        )

        result = asyncio.run(capability.execute(request))

        self.assertTrue(result.output_payload["fallback_used"])
        self.assertIn("降级输出", result.output_payload["summary"])
