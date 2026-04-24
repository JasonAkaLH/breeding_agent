from __future__ import annotations

import asyncio
import unittest

from src.capabilities.sql_query.result_summarize import SQLQueryResultSummarizeCapability

from tests.capabilities.sql_query.support import make_request


class SQLQueryResultSummarizeTest(unittest.TestCase):
    def test_summarizes_query_results(self) -> None:
        capability = SQLQueryResultSummarizeCapability()
        request = make_request(
            "sql_query.result_summarize",
            dependency_outputs={
                "execute": {
                    "columns": ["variety_name"],
                    "rows": [{"variety_name": "龙粳33"}],
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

        capability = SQLQueryResultSummarizeCapability(summarizer=broken)
        request = make_request(
            "sql_query.result_summarize",
            dependency_outputs={
                "execute": {
                    "columns": ["variety_name"],
                    "rows": [{"variety_name": "龙粳33"}],
                    "row_count": 1,
                }
            },
        )

        result = asyncio.run(capability.execute(request))

        self.assertTrue(result.output_payload["fallback_used"])
        self.assertIn("降级输出", result.output_payload["summary"])
