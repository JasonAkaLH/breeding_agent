from __future__ import annotations

import asyncio
import json
import unittest

from src.capabilities.sql_query.result_summarize import SQLQueryResultSummarizeCapability

from tests.capabilities.sql_query.support import make_request


GENERATE_OUTPUT = {
    "route_id": "genotype_db",
    "schema_profile_id": "genotype_profile",
    "user_question": "列出品种",
    "sql": "SELECT variety_name FROM variety LIMIT 50",
    "generation_source": "llm",
}


def request_for_summary(*, rows: list[dict] | None = None, row_count: int | None = None) -> object:
    rows = rows if rows is not None else [{"variety_name": "龙粳33"}]
    return make_request(
        "sql_query.result_summarize",
        dependency_outputs={
            "execute": {
                "sql": "SELECT variety_name FROM variety LIMIT 50",
                "columns": ["variety_name"],
                "rows": rows,
                "row_count": len(rows) if row_count is None else row_count,
            },
            "generate": GENERATE_OUTPUT,
        },
    )


class SQLQueryResultSummarizeLLMTest(unittest.TestCase):
    def test_uses_llm_summary_when_structured_output_is_valid(self) -> None:
        async def llm_text_generator(prompt: str) -> str:
            self.assertIn("列出品种", prompt)
            return json.dumps(
                {
                    "summary": "查询返回 1 行，品种为龙粳33。",
                    "highlights": ["龙粳33"],
                    "row_count": 1,
                    "preview_row_count": 1,
                    "truncated": False,
                    "caveats": [],
                    "summary_source": "llm",
                }
            )

        capability = SQLQueryResultSummarizeCapability(llm_text_generator=llm_text_generator)

        result = asyncio.run(capability.execute(request_for_summary()))

        self.assertEqual(result.output_payload["summary"], "查询返回 1 行，品种为龙粳33。")
        self.assertEqual(result.output_payload["summary_source"], "llm")
        self.assertFalse(result.output_payload["fallback_used"])
        self.assertEqual(result.output_payload["preview_row_count"], 1)
        self.assertFalse(result.output_payload["truncated"])
        self.assertTrue(any(event.event_type == "sql_query.llm_call" for event in result.events))

    def test_zero_rows_uses_deterministic_summary_without_llm_call(self) -> None:
        called = False

        async def llm_text_generator(_: str) -> str:
            nonlocal called
            called = True
            return "{}"

        capability = SQLQueryResultSummarizeCapability(llm_text_generator=llm_text_generator)

        result = asyncio.run(capability.execute(request_for_summary(rows=[], row_count=0)))

        self.assertFalse(called)
        self.assertIn("0 行", result.output_payload["summary"])
        self.assertEqual(result.output_payload["summary_source"], "deterministic")
        self.assertFalse(result.output_payload["fallback_used"])

    def test_summary_prompt_truncates_rows_preview(self) -> None:
        captured_prompts: list[str] = []

        async def llm_text_generator(prompt: str) -> str:
            captured_prompts.append(prompt)
            return json.dumps({"summary": "基于前 20 行预览生成摘要。"})

        rows = [{"variety_name": f"row-{index}"} for index in range(1, 31)]
        capability = SQLQueryResultSummarizeCapability(llm_text_generator=llm_text_generator)

        result = asyncio.run(capability.execute(request_for_summary(rows=rows, row_count=30)))

        self.assertEqual(result.output_payload["preview_row_count"], 20)
        self.assertTrue(result.output_payload["truncated"])
        self.assertIn("row-20", captured_prompts[0])
        self.assertNotIn("row-21", captured_prompts[0])

    def test_invalid_llm_summary_falls_back_to_deterministic_template(self) -> None:
        async def llm_text_generator(_: str) -> str:
            return "not json"

        capability = SQLQueryResultSummarizeCapability(llm_text_generator=llm_text_generator)

        result = asyncio.run(capability.execute(request_for_summary()))

        self.assertEqual(result.output_payload["summary_source"], "fallback")
        self.assertTrue(result.output_payload["fallback_used"])
        self.assertEqual(result.output_payload["fallback_reason"], "parse_failed")
        self.assertIn("降级输出", result.output_payload["summary"])
        self.assertTrue(any(event.event_type == "sql_query.llm_fallback" for event in result.events))


if __name__ == "__main__":
    unittest.main()
