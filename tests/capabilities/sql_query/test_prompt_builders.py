from __future__ import annotations

import unittest

from src.capabilities.sql_query.prompt_builders import (
    build_result_filtering_prompt,
    build_result_filtering_prompt_payload,
    build_sql_generation_prompt,
    build_sql_generation_prompt_payload,
)


class SQLQueryPromptBuildersTest(unittest.TestCase):
    def test_sql_generation_prompt_uses_trimmed_schema_context(self) -> None:
        context = {
            "route_id": "genotype_db",
            "schema_profile_id": "genotype_profile",
            "allowed_tables": ["variety"],
            "selected_tables": ["variety"],
            "selected_columns": {"variety": ["variety_id", "variety_name"]},
            "selected_column_details": {
                "variety": [
                    {"name": "variety_id", "sql_type": "int(11)", "description": "自增ID"},
                    {"name": "variety_name", "sql_type": "varchar(100)", "description": "品种名称"},
                ]
            },
            "join_hints": [],
            "context_summary": "只包含暴露给 LLM 的字段",
            "metadata": {"all_columns": ["internal_secret_column"]},
            "user_question": "查询品种龙粳33的基因型信息",
        }

        payload = build_sql_generation_prompt_payload(context)
        prompt = build_sql_generation_prompt(context)

        self.assertEqual(payload["schema_context"]["selected_columns"], {"variety": ["variety_id", "variety_name"]})
        self.assertEqual(
            payload["schema_context"]["selected_column_details"]["variety"][1],
            {"name": "variety_name", "sql_type": "varchar(100)", "description": "品种名称"},
        )
        self.assertIn("column_types_used", payload["output_contract"]["answer_required_fields"])
        self.assertIn("varchar(100)", prompt)
        self.assertNotIn("internal_secret_column", prompt)
        self.assertIn("readonly_only", prompt)
        self.assertIn("answer | clarify | reject", prompt)
        self.assertIn("JSON", prompt)
        self.assertIn("first_principles_need_inference", payload)
        self.assertIn("第一性原理", prompt)
        self.assertIn("不要假定用户知道该选择哪个库", prompt)
        self.assertIn("variety_name_matching_policy", payload)
        self.assertIn("variety_name", prompt)
        self.assertIn("LIKE", prompt)
        self.assertIn("不得使用", prompt)
        self.assertIn("series_or_contains_pattern", payload["variety_name_matching_policy"])
        self.assertIn("X系列", prompt)

    def test_result_filtering_prompt_uses_candidate_rows_not_full_rows(self) -> None:
        execute_context = {
            "sql": "SELECT variety_name FROM variety WHERE variety_name LIKE '%龙粳33%' LIMIT 50",
            "columns": ["variety_name"],
            "rows": [{"variety_name": f"row-{index}"} for index in range(1, 6)],
            "row_count": 5,
        }
        question_context = {
            "user_question": "查一下龙粳33",
            "route_id": "variety_overview",
            "schema_profile_id": "variety_overview_profile",
        }

        payload = build_result_filtering_prompt_payload(execute_context, question_context=question_context, max_candidate_rows=3)
        prompt = build_result_filtering_prompt(execute_context, question_context=question_context, max_candidate_rows=3)

        self.assertEqual(len(payload["result_context"]["candidate_rows"]), 3)
        self.assertEqual(payload["result_context"]["candidate_rows"][0]["row_index"], 0)
        self.assertTrue(payload["result_context"]["truncated"])
        self.assertNotIn("rows", payload["result_context"])
        self.assertIn("row-3", prompt)
        self.assertNotIn("row-4", prompt)
        self.assertIn("LIKE", prompt)
        self.assertIn("不要总结", prompt)
        self.assertIn("keep_row_indexes", prompt)


if __name__ == "__main__":
    unittest.main()
