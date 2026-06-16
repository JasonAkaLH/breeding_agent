from __future__ import annotations

import _bootstrap  # noqa: F401
import asyncio
import unittest

from sql_query_skill.result_filtering import SQLQueryResultFilteringCapability

from support import make_request


class SQLQueryResultFilteringTest(unittest.TestCase):
    def test_without_llm_keeps_table_shape_and_rows(self) -> None:
        capability = SQLQueryResultFilteringCapability()
        request = make_request(
            "skill.sql_query",
            dependency_outputs={
                "execute": {
                    "columns": ["variety_name", "year"],
                    "rows": [{"variety_name": "龙粳33", "year": 2020}],
                    "row_count": 1,
                }
            },
        )

        result = asyncio.run(capability.execute(request))

        self.assertEqual(result.output_payload["columns"], ["variety_name", "year"])
        self.assertEqual(result.output_payload["rows"], [{"variety_name": "龙粳33", "year": 2020}])
        self.assertEqual(result.output_payload["row_count"], 1)
        self.assertEqual(result.output_payload["filter_source"], "deterministic")
        self.assertFalse(result.output_payload["fallback_used"])
        self.assertEqual(result.output_payload["satisfaction"]["satisfied"], True)
        self.assertEqual(result.output_payload["satisfaction"]["replan_recommended"], False)
        self.assertTrue(any("filtered_query_result" in artifact.artifact_id for artifact in result.artifacts))

    def test_without_llm_filters_numeric_suffix_variants_for_specific_variety(self) -> None:
        capability = SQLQueryResultFilteringCapability()
        request = make_request(
            "skill.sql_query",
            dependency_outputs={
                "execute": {
                    "sql": "SELECT variety_name FROM rice_varieties WHERE variety_name LIKE '%龙粳18%'",
                    "columns": ["variety_name", "approval_num"],
                    "rows": [
                        {"variety_name": "龙粳18号", "approval_num": "黑审稻2007002"},
                        {"variety_name": "龙粳1836", "approval_num": "黑审稻2022L0110"},
                        {"variety_name": "龙粳1823", "approval_num": "黑审稻20220064"},
                        {"variety_name": "龙粳18", "approval_num": None},
                    ],
                    "row_count": 4,
                },
                "generate": {
                    "user_question": "给我查一下龙粳18的信息",
                    "route_id": "approval_variety_db",
                    "schema_profile_id": "approval_variety_profile",
                    "sql": "SELECT variety_name FROM rice_varieties WHERE variety_name LIKE '%龙粳18%'",
                },
            },
        )

        result = asyncio.run(capability.execute(request))

        self.assertEqual(
            result.output_payload["rows"],
            [
                {"variety_name": "龙粳18号", "approval_num": "黑审稻2007002"},
                {"variety_name": "龙粳18", "approval_num": None},
            ],
        )
        self.assertEqual(result.output_payload["kept_row_indexes"], [0, 3])
        self.assertEqual(result.output_payload["row_count"], 2)
        self.assertEqual(result.output_payload["source_row_count"], 4)
        self.assertEqual(result.output_payload["removed_row_count"], 2)
        self.assertTrue(result.output_payload["domain_filter_applied"])

    def test_zero_rows_returns_empty_filtered_table(self) -> None:
        capability = SQLQueryResultFilteringCapability()
        request = make_request(
            "skill.sql_query",
            dependency_outputs={
                "execute": {
                    "columns": ["variety_name"],
                    "rows": [],
                    "row_count": 0,
                }
            },
        )

        result = asyncio.run(capability.execute(request))

        self.assertEqual(result.output_payload["rows"], [])
        self.assertEqual(result.output_payload["row_count"], 0)
        self.assertEqual(result.output_payload["removed_row_count"], 0)
        self.assertEqual(result.output_payload["filter_source"], "deterministic")
        self.assertEqual(result.output_payload["satisfaction"]["satisfied"], False)
        self.assertEqual(result.output_payload["satisfaction"]["reason_code"], "empty_result")
        self.assertEqual(result.output_payload["satisfaction"]["replan_recommended"], True)

    def test_output_self_contains_source_table_summary(self) -> None:
        capability = SQLQueryResultFilteringCapability()
        request = make_request(
            "skill.sql_query",
            dependency_outputs={
                "execute": {
                    "route_id": "approval_variety_db",
                    "schema_profile_id": "approval_variety_profile",
                    "sql": (
                        "SELECT 'rice_varieties' AS source_table, 'rice' AS source_crop, variety_name "
                        "FROM rice_varieties WHERE variety_name LIKE '%龙粳18%'"
                    ),
                    "columns": ["source_table", "source_crop", "variety_name"],
                    "rows": [
                        {"source_table": "rice_varieties", "source_crop": "rice", "variety_name": "龙粳18"},
                    ],
                    "row_count": 1,
                    "selected_tables": ["rice_varieties"],
                    "tables_used": ["rice_varieties"],
                    "source_scope": {"approval_crops": ["rice"]},
                },
                "generate": {
                    "user_question": "给我查一下龙粳18的信息",
                    "route_id": "approval_variety_db",
                    "schema_profile_id": "approval_variety_profile",
                    "selected_tables": ["rice_varieties"],
                    "tables_used": ["rice_varieties"],
                },
            },
        )

        result = asyncio.run(capability.execute(request))

        self.assertEqual(result.output_payload["tables_used"], ["rice_varieties"])
        self.assertIn("rice_varieties", result.output_payload["source_summary"])
        self.assertIn("数据来源", result.output_payload["summary"])
        self.assertIsNone(result.output_payload["no_result_explanation"])

    def test_empty_output_explains_queried_tables_without_details(self) -> None:
        capability = SQLQueryResultFilteringCapability()
        request = make_request(
            "skill.sql_query",
            dependency_outputs={
                "execute": {
                    "route_id": "approval_variety_db",
                    "schema_profile_id": "approval_variety_profile",
                    "columns": ["variety_name"],
                    "rows": [],
                    "row_count": 0,
                    "selected_tables": ["rice_varieties", "corn_varieties"],
                    "tables_used": ["rice_varieties", "corn_varieties"],
                },
            },
        )

        result = asyncio.run(capability.execute(request))

        self.assertEqual(result.output_payload["row_count"], 0)
        self.assertIn("rice_varieties", result.output_payload["no_result_explanation"])
        self.assertIn("corn_varieties", result.output_payload["no_result_explanation"])
        self.assertIn("没有返回匹配结果", result.output_payload["summary"])

    def test_source_summary_includes_entity_match_fields_and_tiers(self) -> None:
        capability = SQLQueryResultFilteringCapability()
        match_summary = {
            "primary": [{"table": "corn_varieties", "field": "applicant", "entity_text": "隆平高科"}],
            "secondary": [{"table": "corn_varieties", "field": "breeder", "entity_text": "隆平高科"}],
            "peer": [],
        }
        request = make_request(
            "skill.sql_query",
            dependency_outputs={
                "execute": {
                    "route_id": "approval_variety_db",
                    "schema_profile_id": "approval_variety_profile",
                    "columns": ["source_table", "source_crop", "matched_field", "match_tier", "variety_name"],
                    "rows": [
                        {
                            "source_table": "corn_varieties",
                            "source_crop": "corn",
                            "matched_field": "applicant",
                            "match_tier": "primary",
                            "variety_name": "测试玉米",
                        }
                    ],
                    "row_count": 1,
                    "selected_tables": ["corn_varieties"],
                    "tables_used": ["corn_varieties"],
                    "source_scope": {"approval_crops": ["corn"], "matched_fields": ["applicant", "breeder"], "match_tiers": ["primary", "secondary"]},
                    "match_summary": match_summary,
                    "matched_fields": ["applicant", "breeder"],
                    "match_tiers": ["primary", "secondary"],
                    "search_effort_summary": "我已在玉米审定品种表中，按申请者、育种者字段查找“隆平高科”",
                }
            },
        )

        result = asyncio.run(capability.execute(request))

        self.assertIn("主要命中字段：申请者", result.output_payload["source_summary"])
        self.assertIn("附带命中字段：育种者", result.output_payload["source_summary"])
        self.assertEqual(result.output_payload["match_summary"], match_summary)
        self.assertEqual(result.output_payload["matched_fields"], ["applicant", "breeder"])


    def test_source_summary_includes_constraint_summary(self) -> None:
        capability = SQLQueryResultFilteringCapability()
        request = make_request(
            "skill.sql_query",
            dependency_outputs={
                "execute": {
                    "route_id": "approval_variety_db",
                    "schema_profile_id": "approval_variety_profile",
                    "columns": ["source_table", "source_crop", "year", "variety_name"],
                    "rows": [
                        {"source_table": "rice_varieties", "source_crop": "rice", "year": 2021, "variety_name": "测试水稻"},
                    ],
                    "row_count": 1,
                    "selected_tables": ["rice_varieties"],
                    "tables_used": ["rice_varieties"],
                    "source_scope": {"approval_crops": ["rice"]},
                    "query_constraints": {"constraint_summary": "年份为 2021；适种区域包含 河南"},
                    "constraint_coverage_summary": {"covered": True, "summary": "年份为 2021；适种区域包含 河南"},
                }
            },
        )

        result = asyncio.run(capability.execute(request))

        self.assertIn("查询约束：年份为 2021", result.output_payload["source_summary"])
        self.assertIn("适种区域包含 河南", result.output_payload["summary"])
        self.assertEqual(result.output_payload["query_constraints"]["constraint_summary"], "年份为 2021；适种区域包含 河南")
        self.assertTrue(result.output_payload["constraint_coverage_summary"]["covered"])

    def test_empty_entity_probe_output_uses_search_effort_summary(self) -> None:
        capability = SQLQueryResultFilteringCapability()
        request = make_request(
            "skill.sql_query",
            dependency_outputs={
                "execute": {
                    "route_id": "approval_variety_db",
                    "schema_profile_id": "approval_variety_profile",
                    "columns": ["variety_name"],
                    "rows": [],
                    "row_count": 0,
                    "selected_tables": ["corn_varieties"],
                    "tables_used": ["corn_varieties"],
                    "search_effort_summary": "我已在玉米审定品种表中，按申请者字段查找“隆平高科”",
                }
            },
        )

        result = asyncio.run(capability.execute(request))

        self.assertIn("按申请者字段查找", result.output_payload["no_result_explanation"])
        self.assertIn("没有返回匹配记录", result.output_payload["no_result_explanation"])


if __name__ == "__main__":
    unittest.main()
