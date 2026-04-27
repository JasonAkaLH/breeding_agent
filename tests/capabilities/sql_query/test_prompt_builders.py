from __future__ import annotations

import unittest

from src.capabilities.sql_query.prompt_builders import (
    build_result_summary_prompt,
    build_result_summary_prompt_payload,
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

    def test_summary_prompt_uses_rows_preview_not_full_rows(self) -> None:
        execute_context = {
            "sql": "SELECT variety_name FROM variety LIMIT 50",
            "columns": ["variety_name"],
            "rows": [{"variety_name": f"row-{index}"} for index in range(1, 6)],
            "row_count": 5,
        }
        question_context = {
            "user_question": "列出品种",
            "route_id": "genotype_db",
            "schema_profile_id": "genotype_profile",
        }

        payload = build_result_summary_prompt_payload(execute_context, question_context=question_context, max_preview_rows=3)
        prompt = build_result_summary_prompt(execute_context, question_context=question_context, max_preview_rows=3)

        self.assertEqual(len(payload["result_context"]["rows_preview"]), 3)
        self.assertTrue(payload["result_context"]["truncated"])
        self.assertNotIn("rows", payload["result_context"])
        self.assertIn("row-3", prompt)
        self.assertNotIn("row-4", prompt)
        self.assertIn("不要编造", prompt)


if __name__ == "__main__":
    unittest.main()
