from __future__ import annotations

import _bootstrap  # noqa: F401
import asyncio
import json
import unittest

from sql_query_skill.result_filtering import SQLQueryResultFilteringCapability
from src.integrations.token_counter import get_num_of_tokens_from_messages

from support import make_request


GENERATE_OUTPUT = {
    "route_id": "approval_variety_db",
    "schema_profile_id": "approval_variety_profile",
    "user_question": "查询龙粳33审定信息",
    "sql": "SELECT source_db, variety_name FROM rice_varieties WHERE variety_name LIKE '%龙粳33%'",
    "generation_source": "llm",
}


def request_for_filtering(*, rows: list[dict] | None = None, row_count: int | None = None) -> object:
    rows = rows if rows is not None else [
        {"source_db": "approval", "variety_name": "龙粳33"},
        {"source_db": "approval", "variety_name": "龙粳331"},
        {"source_db": "genotype", "variety_name": "龙粳33"},
    ]
    return make_request(
        "skill.sql_query",
        dependency_outputs={
            "execute": {
                "sql": "SELECT source_db, variety_name FROM rice_varieties WHERE variety_name LIKE '%龙粳33%'",
                "columns": ["source_db", "variety_name"],
                "rows": rows,
                "row_count": len(rows) if row_count is None else row_count,
            },
            "generate": GENERATE_OUTPUT,
        },
    )


class SQLQueryResultFilteringLLMTest(unittest.TestCase):
    def test_uses_llm_keep_row_indexes_to_filter_mismatched_names(self) -> None:
        async def llm_text_generator(prompt: str) -> str:
            self.assertIn('"stage": "result_filtering"', prompt)
            self.assertIn("查询龙粳33审定信息", prompt)
            self.assertIn("LIKE", prompt)
            return json.dumps({"keep_row_indexes": [0, 2], "filter_reason": "保留品种名精确对应龙粳33的候选行。"})

        capability = SQLQueryResultFilteringCapability(llm_text_generator=llm_text_generator)

        result = asyncio.run(capability.execute(request_for_filtering()))

        self.assertEqual(
            result.output_payload["rows"],
            [
                {"source_db": "approval", "variety_name": "龙粳33"},
                {"source_db": "genotype", "variety_name": "龙粳33"},
            ],
        )
        self.assertEqual(result.output_payload["kept_row_indexes"], [0, 2])
        self.assertEqual(result.output_payload["row_count"], 2)
        self.assertEqual(result.output_payload["source_row_count"], 3)
        self.assertEqual(result.output_payload["removed_row_count"], 1)
        self.assertEqual(result.output_payload["filter_source"], "llm")
        self.assertFalse(result.output_payload["fallback_used"])
        self.assertTrue(any(event.event_type == "skill.llm_call" for event in result.events))

    def test_domain_exact_filter_removes_numeric_suffix_even_if_llm_keeps_it(self) -> None:
        async def llm_text_generator(_: str) -> str:
            return json.dumps({"keep_row_indexes": [0, 1, 2], "filter_reason": "模型误保留全部候选。"})

        capability = SQLQueryResultFilteringCapability(llm_text_generator=llm_text_generator)

        result = asyncio.run(
            capability.execute(
                make_request(
                    "skill.sql_query",
                    dependency_outputs={
                        "execute": {
                            "sql": "SELECT source_db, variety_name FROM rice_varieties WHERE variety_name LIKE '%龙粳18%'",
                            "columns": ["source_db", "variety_name"],
                            "rows": [
                                {"source_db": "approval", "variety_name": "龙粳18号"},
                                {"source_db": "approval", "variety_name": "龙粳1836"},
                                {"source_db": "genotype", "variety_name": "龙粳18"},
                            ],
                            "row_count": 3,
                        },
                        "generate": {
                            "route_id": "approval_variety_db",
                            "schema_profile_id": "approval_variety_profile",
                            "user_question": "查询龙粳18审定信息",
                            "sql": "SELECT source_db, variety_name FROM rice_varieties WHERE variety_name LIKE '%龙粳18%'",
                        },
                    },
                )
            )
        )

        self.assertEqual(
            result.output_payload["rows"],
            [
                {"source_db": "approval", "variety_name": "龙粳18号"},
                {"source_db": "genotype", "variety_name": "龙粳18"},
            ],
        )
        self.assertEqual(result.output_payload["kept_row_indexes"], [0, 2])
        self.assertTrue(result.output_payload["domain_filter_applied"])

    def test_prompt_sends_all_trimmed_rows_without_hard_candidate_cap(self) -> None:
        captured_prompts: list[str] = []

        async def llm_text_generator(prompt: str) -> str:
            captured_prompts.append(prompt)
            return json.dumps({"keep_row_indexes": [0, 200, 204]})

        rows = [{"variety_name": f"row-{index}"} for index in range(1, 206)]
        capability = SQLQueryResultFilteringCapability(llm_text_generator=llm_text_generator)

        result = asyncio.run(capability.execute(request_for_filtering(rows=rows, row_count=205)))

        self.assertEqual(result.output_payload["candidate_row_count"], 205)
        self.assertFalse(result.output_payload["truncated"])
        self.assertIn("row-205", captured_prompts[0])
        self.assertEqual(
            result.output_payload["rows"],
            [
                {"variety_name": "row-1"},
                {"variety_name": "row-201"},
                {"variety_name": "row-205"},
            ],
        )

    def test_token_trim_keeps_latest_rows_before_llm_prompt(self) -> None:
        captured_prompts: list[str] = []

        async def llm_text_generator(prompt: str) -> str:
            captured_prompts.append(prompt)
            return json.dumps({"keep_row_indexes": [0, 1]})

        rows = [
            {"variety_name": "old-row", "detail": "older data " * 20},
            {"variety_name": "middle-row", "detail": "middle data"},
            {"variety_name": "new-row", "detail": "new data"},
        ]
        latest_two_token_budget = sum(
            get_num_of_tokens_from_messages([json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)])
            for row in rows[-2:]
        )
        capability = SQLQueryResultFilteringCapability(
            llm_text_generator=llm_text_generator,
            trim_max_tokens=latest_two_token_budget,
        )

        result = asyncio.run(capability.execute(request_for_filtering(rows=rows, row_count=3)))

        self.assertIn("new-row", captured_prompts[0])
        self.assertIn("middle-row", captured_prompts[0])
        self.assertNotIn("old-row", captured_prompts[0])
        self.assertEqual(
            result.output_payload["rows"],
            [
                {"variety_name": "new-row", "detail": "new data"},
                {"variety_name": "middle-row", "detail": "middle data"},
            ],
        )
        self.assertTrue(result.output_payload["token_trim_applied"])
        self.assertEqual(result.output_payload["token_trim_removed_row_count"], 1)
        self.assertEqual(result.output_payload["candidate_row_count"], 2)
        self.assertTrue(result.output_payload["truncated"])

    def test_invalid_llm_indexes_fall_back_to_domain_filtered_table(self) -> None:
        async def llm_text_generator(_: str) -> str:
            return json.dumps({"keep_row_indexes": [99]})

        capability = SQLQueryResultFilteringCapability(llm_text_generator=llm_text_generator)

        result = asyncio.run(capability.execute(request_for_filtering()))

        self.assertEqual(result.output_payload["filter_source"], "fallback")
        self.assertTrue(result.output_payload["fallback_used"])
        self.assertEqual(result.output_payload["fallback_reason"], "validation_failed")
        self.assertEqual(result.output_payload["row_count"], 2)
        self.assertEqual(result.output_payload["kept_row_indexes"], [0, 2])
        self.assertTrue(result.output_payload["domain_filter_applied"])
        self.assertTrue(any(event.event_type == "skill.llm_fallback" for event in result.events))

    def test_invalid_json_falls_back_to_unfiltered_table(self) -> None:
        async def llm_text_generator(_: str) -> str:
            return "not json"

        capability = SQLQueryResultFilteringCapability(llm_text_generator=llm_text_generator)

        result = asyncio.run(capability.execute(request_for_filtering()))

        self.assertEqual(result.output_payload["filter_source"], "fallback")
        self.assertTrue(result.output_payload["fallback_used"])
        self.assertEqual(result.output_payload["rows"][0]["variety_name"], "龙粳33")


if __name__ == "__main__":
    unittest.main()
