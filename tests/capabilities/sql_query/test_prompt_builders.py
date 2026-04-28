from __future__ import annotations

import unittest

from src.capabilities.sql_query.prompt_builders import (
    build_result_filtering_prompt,
    build_result_filtering_prompt_payload,
    build_sql_generation_prompt,
    build_sql_generation_prompt_payload,
)


class SQLQueryPromptBuildersTest(unittest.TestCase):
    def test_sql_generation_prompt_uses_xiaoao_style_gene_schema_prompt(self) -> None:
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
            "schema_ddl": (
                "-- ----------------------------\n"
                "-- 表结构：variety\n"
                "-- ----------------------------\n"
                "CREATE TABLE `variety`  (\n"
                "`variety_id` int(11) COMMENT '自增ID',\n"
                "`variety_name` varchar(100) COMMENT '品种名称'\n"
                ") COMMENT = '水稻品种信息表';"
            ),
            "user_question": "查询品种龙粳33的基因型信息",
        }

        payload = build_sql_generation_prompt_payload(context)
        prompt = build_sql_generation_prompt(context)

        self.assertEqual(payload["schema_context"]["selected_columns"], {"variety": ["variety_id", "variety_name"]})
        self.assertIn("database_schema", payload["schema_context"])
        self.assertEqual(
            payload["schema_context"]["selected_column_details"]["variety"][1],
            {"name": "variety_name", "sql_type": "varchar(100)", "description": "品种名称"},
        )
        self.assertEqual(payload["output_contract"]["format"], "仅 SQL")
        self.assertEqual(payload["output_contract"]["llm_output"], "只输出 SQL 查询语句，不输出 JSON、Markdown 或解释。")
        self.assertIn("生成一个SQL查询来回答这个问题", prompt)
        self.assertIn("## 以下是数据库结构", prompt)
        self.assertIn("表结构：variety", prompt)
        self.assertIn("varchar(100)", prompt)
        self.assertNotIn("internal_secret_column", prompt)
        self.assertIn("readonly_only", payload["guard_constraints"])
        self.assertNotIn("require_limit_for_non_aggregate_query", payload["guard_constraints"])
        self.assertNotIn("max_limit", payload["guard_constraints"])
        self.assertIn("不要自动添加 LIMIT", prompt)
        self.assertNotIn("非聚合查询必须包含 LIMIT", prompt)
        self.assertIn("first_principles_need_inference", payload)
        self.assertIn("variety_name_matching_policy", payload)
        self.assertIn("variety_name", prompt)
        self.assertIn("LIKE", prompt)
        self.assertIn("你只需要输出SQL语句", prompt)
        self.assertIn("连接关系", prompt)

    def test_approval_prompt_uses_varieties_template_and_only_injected_crop_table(self) -> None:
        context = {
            "route_id": "approval_variety_db",
            "schema_profile_id": "approval_variety_profile",
            "allowed_tables": ["corn_varieties", "rice_varieties"],
            "selected_tables": ["rice_varieties"],
            "selected_columns": {
                "rice_varieties": ["year", "variety_name", "suitable_area", "applicant", "breeder"],
            },
            "selected_column_details": {
                "rice_varieties": [
                    {"name": "year", "sql_type": "int(11)", "description": "年份"},
                    {"name": "variety_name", "sql_type": "varchar(100)", "description": "品种名称"},
                    {"name": "suitable_area", "sql_type": "text", "description": "适种区域"},
                    {"name": "applicant", "sql_type": "varchar(200)", "description": "申请者"},
                    {"name": "breeder", "sql_type": "varchar(200)", "description": "育种者"},
                ],
            },
            "schema_ddl": (
                "-- ----------------------------\n"
                "-- 表结构：rice_varieties\n"
                "-- ----------------------------\n"
                "CREATE TABLE `rice_varieties`  (\n"
                "`year` int(11) COMMENT '年份',\n"
                "`variety_name` varchar(100) COMMENT '品种名称',\n"
                "`suitable_area` text COMMENT '适种区域'\n"
                ") COMMENT = '水稻审定品种表';"
            ),
            "user_question": "给我查一下适合河南种植的水稻",
        }

        prompt = build_sql_generation_prompt(context)

        self.assertIn("作物与品种表的对应关系如下", prompt)
        self.assertIn("CREATE TABLE `rice_varieties`", prompt)
        self.assertIn("表结构：rice_varieties", prompt)
        self.assertNotIn("CREATE TABLE `corn_varieties`", prompt)
        self.assertIn("你需要输出字段的注释作为列名", prompt)
        self.assertIn("applicant", prompt)
        self.assertIn("breeder", prompt)
        self.assertIn("不要自动添加 LIMIT", prompt)
        self.assertIn("只需要输出SQL语句", prompt)

    def test_result_filtering_prompt_uses_all_rows_after_token_trim(self) -> None:
        execute_context = {
            "sql": "SELECT variety_name FROM variety WHERE variety_name LIKE '%龙粳33%'",
            "columns": ["variety_name"],
            "rows": [{"variety_name": f"row-{index}"} for index in range(1, 6)],
            "row_count": 5,
        }
        question_context = {
            "user_question": "查一下龙粳33",
            "route_id": "variety_overview",
            "schema_profile_id": "variety_overview_profile",
        }

        payload = build_result_filtering_prompt_payload(execute_context, question_context=question_context)
        prompt = build_result_filtering_prompt(execute_context, question_context=question_context)

        self.assertEqual(len(payload["result_context"]["candidate_rows"]), 5)
        self.assertEqual(payload["result_context"]["candidate_rows"][0]["row_index"], 0)
        self.assertFalse(payload["result_context"]["truncated"])
        self.assertNotIn("rows", payload["result_context"])
        self.assertIn("row-5", prompt)
        self.assertIn("LIKE", prompt)
        self.assertIn("不要总结", prompt)
        self.assertIn("keep_row_indexes", prompt)

    def test_result_filtering_prompt_marks_truncated_when_rows_were_token_trimmed(self) -> None:
        execute_context = {
            "sql": "SELECT variety_name FROM variety WHERE variety_name LIKE '%龙粳33%'",
            "columns": ["variety_name"],
            "rows": [{"variety_name": f"new-row-{index}"} for index in range(1, 4)],
            "row_count": 5,
        }

        payload = build_result_filtering_prompt_payload(execute_context)
        prompt = build_result_filtering_prompt(execute_context)

        self.assertEqual(payload["result_context"]["candidate_row_count"], 3)
        self.assertTrue(payload["result_context"]["truncated"])
        self.assertIn("new-row-3", prompt)


if __name__ == "__main__":
    unittest.main()
