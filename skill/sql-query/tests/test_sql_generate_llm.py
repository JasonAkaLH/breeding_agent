from __future__ import annotations

import _bootstrap  # noqa: F401
import asyncio
import json
import unittest

from sql_query_skill.query_constraints import build_query_constraints
from sql_query_skill.sql_generate import SQLQuerySQLGenerateCapability

from support import make_request


SCHEMA_CONTEXT = {
    "route_id": "genotype_db",
    "schema_profile_id": "genotype_profile",
    "sql_policy_profile": "strict_readonly_mysql",
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
    "context_summary": "variety 表",
    "user_question": "查询品种龙粳33的基因型信息",
}

APPROVAL_DETAIL_SCHEMA_CONTEXT = {
    "route_id": "approval_variety_db",
    "schema_profile_id": "approval_variety_profile",
    "sql_policy_profile": "strict_readonly_mysql",
    "allowed_tables": ["rice_varieties"],
    "selected_tables": ["rice_varieties"],
    "selected_columns": {
        "rice_varieties": [
            "year",
            "approval_num",
            "crop_name",
            "variety_name",
            "applicant",
            "breeder",
            "variety_source",
            "characteristics",
            "yield_performance",
            "cultivation_tips",
            "approval_opinion",
            "suitable_area",
        ]
    },
    "selected_column_details": {
        "rice_varieties": [
            {"name": "year", "sql_type": "int(11)", "description": "年份"},
            {"name": "approval_num", "sql_type": "varchar(100)", "description": "审定编号"},
            {"name": "crop_name", "sql_type": "varchar(20)", "description": "作物名称"},
            {"name": "variety_name", "sql_type": "varchar(100)", "description": "品种名称"},
            {"name": "applicant", "sql_type": "varchar(200)", "description": "申请者"},
            {"name": "breeder", "sql_type": "varchar(200)", "description": "育种者"},
            {"name": "variety_source", "sql_type": "text", "description": "品种来源"},
            {"name": "characteristics", "sql_type": "text", "description": "特征特性"},
            {"name": "yield_performance", "sql_type": "text", "description": "产量表现"},
            {"name": "cultivation_tips", "sql_type": "text", "description": "栽培技术要点"},
            {"name": "approval_opinion", "sql_type": "text", "description": "审定意见"},
            {"name": "suitable_area", "sql_type": "text", "description": "适种区域"},
        ]
    },
    "join_hints": [],
    "context_summary": "水稻审定品种详情",
    "user_question": "龙粳18的详细审定信息，要所有信息",
}


def request_for_sql_generate() -> object:
    return make_request(
        "skill.sql_query",
        dependency_outputs={"schema": SCHEMA_CONTEXT},
    )


def request_for_approval_detail_sql_generate() -> object:
    return make_request(
        "skill.sql_query",
        dependency_outputs={"schema": APPROVAL_DETAIL_SCHEMA_CONTEXT},
    )


def request_for_approval_detail_sql_generate_with_question(user_question: str) -> object:
    return make_request(
        "skill.sql_query",
        dependency_outputs={"schema": {**APPROVAL_DETAIL_SCHEMA_CONTEXT, "user_question": user_question}},
    )


class SQLQuerySQLGenerateLLMTest(unittest.TestCase):
    def test_uses_llm_answer_when_structured_output_is_valid(self) -> None:
        async def llm_text_generator(prompt: str) -> str:
            self.assertIn("genotype_db", prompt)
            self.assertIn("生成一个SQL查询来回答这个问题", prompt)
            return json.dumps(
                {
                    "mode": "answer",
                    "route_id": "genotype_db",
                    "schema_profile_id": "genotype_profile",
                    "sql": "SELECT variety_name FROM variety",
                    "tables_used": ["variety"],
                    "columns_used": ["variety.variety_name"],
                    "column_types_used": {"variety.variety_name": "varchar(100)"},
                    "join_hints_used": [],
                }
            )

        capability = SQLQuerySQLGenerateCapability(llm_text_generator=llm_text_generator)

        result = asyncio.run(capability.execute(request_for_sql_generate()))

        self.assertIsNone(result.error)
        self.assertIsNone(result.interrupt)
        self.assertEqual(result.output_payload["sql"], "SELECT variety_name FROM variety")
        self.assertEqual(result.output_payload["generation_source"], "llm")
        self.assertEqual(result.output_payload["llm_mode"], "answer")
        self.assertFalse(result.output_payload["fallback_used"])
        self.assertEqual(result.output_payload["tables_used"], ["variety"])
        self.assertEqual(result.output_payload["column_types_used"], {"variety.variety_name": "varchar(100)"})
        self.assertTrue(any(event.event_type == "skill.llm_call" for event in result.events))

    def test_accepts_xiaoao_style_raw_sql_output_and_infers_schema_usage(self) -> None:
        async def llm_text_generator(prompt: str) -> str:
            self.assertIn("你只需要输出SQL语句", prompt)
            self.assertNotIn('"mode"', prompt)
            return (
                "```sql\n"
                "SELECT rice_varieties.year AS '年份', "
                "rice_varieties.variety_name AS '品种名称', "
                "rice_varieties.suitable_area AS '适种区域' "
                "FROM rice_varieties "
                "WHERE rice_varieties.suitable_area LIKE '%河南%'\n"
                "```"
            )

        capability = SQLQuerySQLGenerateCapability(llm_text_generator=llm_text_generator)

        result = asyncio.run(
            capability.execute(
                request_for_approval_detail_sql_generate_with_question(
                    "给我查一下适合河南种植的水稻"
                )
            )
        )

        self.assertIsNone(result.error)
        self.assertIsNone(result.interrupt)
        self.assertEqual(result.output_payload["generation_source"], "llm")
        self.assertEqual(result.output_payload["llm_mode"], "answer")
        self.assertFalse(result.output_payload["fallback_used"])
        self.assertEqual(result.output_payload["tables_used"], ["rice_varieties"])
        self.assertEqual(
            result.output_payload["columns_used"],
            [
                "rice_varieties.year",
                "rice_varieties.variety_name",
                "rice_varieties.suitable_area",
            ],
        )
        self.assertEqual(
            result.output_payload["column_types_used"],
            {
                "rice_varieties.year": "int(11)",
                "rice_varieties.variety_name": "varchar(100)",
                "rice_varieties.suitable_area": "text",
            },
        )
        self.assertIn("suitable_area LIKE '%河南%'", result.output_payload["sql"])
        self.assertNotIn("```", result.output_payload["sql"])

    def test_llm_selects_projection_and_where_columns_without_required_type_echo(self) -> None:
        async def llm_text_generator(prompt: str) -> str:
            self.assertIn("suitable_area", prompt)
            return json.dumps(
                {
                    "mode": "answer",
                    "route_id": "approval_variety_db",
                    "schema_profile_id": "approval_variety_profile",
                    "sql": (
                        "SELECT rice_varieties.year, rice_varieties.variety_name, rice_varieties.suitable_area "
                        "FROM rice_varieties "
                        "WHERE rice_varieties.suitable_area LIKE '%河南%'"
                    ),
                    "tables_used": ["rice_varieties"],
                    "columns_used": [
                        "rice_varieties.year",
                        "rice_varieties.variety_name",
                        "rice_varieties.suitable_area",
                    ],
                    "join_hints_used": [],
                },
                ensure_ascii=False,
            )

        capability = SQLQuerySQLGenerateCapability(llm_text_generator=llm_text_generator)

        result = asyncio.run(
            capability.execute(
                request_for_approval_detail_sql_generate_with_question(
                    "给我查一下适合河南种植的水稻"
                )
            )
        )

        self.assertEqual(result.output_payload["generation_source"], "llm")
        self.assertFalse(result.output_payload["fallback_used"])
        self.assertIn("suitable_area LIKE '%河南%'", result.output_payload["sql"])
        self.assertEqual(
            result.output_payload["column_types_used"],
            {
                "rice_varieties.year": "int(11)",
                "rice_varieties.variety_name": "varchar(100)",
                "rice_varieties.suitable_area": "text",
            },
        )

    def test_fallback_generates_like_filter_for_named_variety_series(self) -> None:
        capability = SQLQuerySQLGenerateCapability()

        result = asyncio.run(
            capability.execute(
                request_for_approval_detail_sql_generate_with_question(
                    "给我查一下龙粳系列的信息，龙粳系列指的是名字里带“龙粳”的品种"
                )
            )
        )

        self.assertIn("rice_varieties.variety_name LIKE '%龙粳%'", result.output_payload["sql"])

    def test_fallback_generates_like_filter_for_series_question_without_definition(self) -> None:
        capability = SQLQuerySQLGenerateCapability()

        result = asyncio.run(
            capability.execute(
                request_for_approval_detail_sql_generate_with_question(
                    "给我再查一下龙粳系列都有什么品种？"
                )
            )
        )

        self.assertIn("rice_varieties.variety_name LIKE '%龙粳%'", result.output_payload["sql"])

    def test_fallback_uses_entity_match_summary_for_primary_and_secondary_filters(self) -> None:
        capability = SQLQuerySQLGenerateCapability()
        context = {
            **APPROVAL_DETAIL_SCHEMA_CONTEXT,
            "user_question": "隆平高科申请的审定品种",
            "entities": [{"text": "隆平高科", "entity_type": "organization", "field_intent": "applicant"}],
            "match_summary": {
                "primary": [{"table": "rice_varieties", "field": "applicant", "entity_text": "隆平高科"}],
                "secondary": [{"table": "rice_varieties", "field": "breeder", "entity_text": "隆平高科"}],
                "peer": [],
            },
            "matched_fields": ["applicant", "breeder"],
            "match_tiers": ["primary", "secondary"],
        }

        result = asyncio.run(
            capability.execute(
                make_request("skill.sql_query", dependency_outputs={"schema": context})
            )
        )

        sql = result.output_payload["sql"]
        self.assertIsNone(result.error)
        self.assertIn("'applicant' AS matched_field", sql)
        self.assertIn("'primary' AS match_tier", sql)
        self.assertIn("rice_varieties.applicant LIKE '%隆平高科%'", sql)
        self.assertIn("'breeder' AS matched_field", sql)
        self.assertIn("'secondary' AS match_tier", sql)
        self.assertIn("rice_varieties.breeder LIKE '%隆平高科%'", sql)
        self.assertIn(" UNION ALL ", sql)
        self.assertEqual(result.output_payload["matched_fields"], ["applicant", "breeder"])

    def test_llm_sql_missing_secondary_entity_filter_falls_back(self) -> None:
        async def llm_text_generator(_: str) -> str:
            return (
                "SELECT 'rice_varieties' AS source_table, 'rice' AS source_crop, "
                "'applicant' AS matched_field, 'primary' AS match_tier, rice_varieties.variety_name "
                "FROM rice_varieties WHERE rice_varieties.applicant LIKE '%隆平高科%'"
            )

        capability = SQLQuerySQLGenerateCapability(llm_text_generator=llm_text_generator)
        context = {
            **APPROVAL_DETAIL_SCHEMA_CONTEXT,
            "user_question": "隆平高科申请的审定品种",
            "match_summary": {
                "primary": [{"table": "rice_varieties", "field": "applicant", "entity_text": "隆平高科"}],
                "secondary": [{"table": "rice_varieties", "field": "breeder", "entity_text": "隆平高科"}],
                "peer": [],
            },
            "matched_fields": ["applicant", "breeder"],
        }

        result = asyncio.run(
            capability.execute(
                make_request("skill.sql_query", dependency_outputs={"schema": context})
            )
        )

        self.assertEqual(result.output_payload["generation_source"], "fallback")
        self.assertEqual(result.output_payload["llm_mode"], "validation_failed")
        self.assertIn("rice_varieties.breeder LIKE '%隆平高科%'", result.output_payload["sql"])

    def test_llm_or_only_entity_sql_falls_back_to_union_branches(self) -> None:
        async def llm_text_generator(_: str) -> str:
            return (
                "SELECT 'rice_varieties' AS source_table, 'rice' AS source_crop, "
                "'applicant' AS matched_field, 'primary' AS match_tier, rice_varieties.variety_name "
                "FROM rice_varieties "
                "WHERE rice_varieties.applicant LIKE '%隆平高科%' OR rice_varieties.breeder LIKE '%隆平高科%'"
            )

        capability = SQLQuerySQLGenerateCapability(llm_text_generator=llm_text_generator)
        context = {
            **APPROVAL_DETAIL_SCHEMA_CONTEXT,
            "user_question": "隆平高科申请的审定品种",
            "match_summary": {
                "primary": [{"table": "rice_varieties", "field": "applicant", "entity_text": "隆平高科"}],
                "secondary": [{"table": "rice_varieties", "field": "breeder", "entity_text": "隆平高科"}],
                "peer": [],
            },
            "matched_fields": ["applicant", "breeder"],
        }

        result = asyncio.run(
            capability.execute(
                make_request("skill.sql_query", dependency_outputs={"schema": context})
            )
        )

        self.assertEqual(result.output_payload["generation_source"], "fallback")
        self.assertEqual(result.output_payload["llm_mode"], "validation_failed")
        self.assertIn(" UNION ALL ", result.output_payload["sql"])
        self.assertIn("'applicant' AS matched_field", result.output_payload["sql"])
        self.assertIn("'breeder' AS matched_field", result.output_payload["sql"])

    def test_selected_tables_are_required_for_sql_generation(self) -> None:
        capability = SQLQuerySQLGenerateCapability()
        context = {
            **APPROVAL_DETAIL_SCHEMA_CONTEXT,
            "allowed_tables": ["rice_varieties"],
            "selected_tables": [],
        }

        result = asyncio.run(
            capability.execute(
                make_request("skill.sql_query", dependency_outputs={"schema": context})
            )
        )

        self.assertIsNotNone(result.error)
        self.assertEqual(result.error.code, "selected_tables_required")


    def test_invalid_llm_column_name_falls_back_to_heuristic_generator(self) -> None:
        async def llm_text_generator(_: str) -> str:
            return json.dumps(
                {
                    "mode": "answer",
                    "route_id": "genotype_db",
                    "schema_profile_id": "genotype_profile",
                    "sql": "SELECT fake_column FROM variety",
                    "tables_used": ["variety"],
                    "columns_used": ["variety.fake_column"],
                    "column_types_used": {"variety.fake_column": "varchar(100)"},
                }
            )

        capability = SQLQuerySQLGenerateCapability(
            llm_text_generator=llm_text_generator,
            generator=lambda _: "SELECT variety_name FROM variety",
        )

        result = asyncio.run(capability.execute(request_for_sql_generate()))

        self.assertIsNone(result.error)
        self.assertEqual(result.output_payload["generation_source"], "fallback")
        self.assertEqual(result.output_payload["llm_mode"], "validation_failed")

    def test_invalid_llm_column_type_falls_back_to_heuristic_generator(self) -> None:
        async def llm_text_generator(_: str) -> str:
            return json.dumps(
                {
                    "mode": "answer",
                    "route_id": "genotype_db",
                    "schema_profile_id": "genotype_profile",
                    "sql": "SELECT variety_name FROM variety",
                    "tables_used": ["variety"],
                    "columns_used": ["variety.variety_name"],
                    "column_types_used": {"variety.variety_name": "text"},
                }
            )

        capability = SQLQuerySQLGenerateCapability(
            llm_text_generator=llm_text_generator,
            generator=lambda _: "SELECT variety_name FROM variety",
        )

        result = asyncio.run(capability.execute(request_for_sql_generate()))

        self.assertIsNone(result.error)
        self.assertEqual(result.output_payload["generation_source"], "fallback")
        self.assertEqual(result.output_payload["llm_mode"], "validation_failed")

    def test_invalid_llm_output_falls_back_to_heuristic_generator(self) -> None:
        async def llm_text_generator(_: str) -> str:
            return "not json"

        capability = SQLQuerySQLGenerateCapability(
            llm_text_generator=llm_text_generator,
            generator=lambda _: "SELECT variety_name FROM variety",
        )

        result = asyncio.run(capability.execute(request_for_sql_generate()))

        self.assertIsNone(result.error)
        self.assertEqual(result.output_payload["sql"], "SELECT variety_name FROM variety")
        self.assertEqual(result.output_payload["generation_source"], "fallback")
        self.assertEqual(result.output_payload["llm_mode"], "parse_failed")
        self.assertTrue(result.output_payload["fallback_used"])
        self.assertIn("fallback_reason", result.output_payload)
        self.assertTrue(any(event.event_type == "skill.llm_fallback" for event in result.events))

    def test_llm_variety_name_strict_equals_falls_back_to_like_matching(self) -> None:
        async def llm_text_generator(_: str) -> str:
            return json.dumps(
                {
                    "mode": "answer",
                    "route_id": "genotype_db",
                    "schema_profile_id": "genotype_profile",
                    "sql": "SELECT variety_name FROM variety WHERE variety_name = '龙粳33'",
                    "tables_used": ["variety"],
                    "columns_used": ["variety.variety_name"],
                    "column_types_used": {"variety.variety_name": "varchar(100)"},
                }
            )

        capability = SQLQuerySQLGenerateCapability(llm_text_generator=llm_text_generator)

        result = asyncio.run(capability.execute(request_for_sql_generate()))

        sql = result.output_payload["sql"]
        self.assertEqual(result.output_payload["generation_source"], "fallback")
        self.assertEqual(result.output_payload["llm_mode"], "validation_failed")
        self.assertIn("variety_name LIKE '%龙粳33%'", sql)
        self.assertNotIn("variety_name =", sql)

    def test_fallback_filters_generic_variety_name_with_like(self) -> None:
        capability = SQLQuerySQLGenerateCapability()

        result = asyncio.run(capability.execute(request_for_sql_generate()))

        sql = result.output_payload["sql"]
        self.assertIn("variety_name LIKE '%龙粳33%'", sql)
        self.assertNotIn("variety_name =", sql)

    def test_fallback_generates_rich_approval_detail_sql_for_single_variety(self) -> None:
        capability = SQLQuerySQLGenerateCapability()

        result = asyncio.run(capability.execute(request_for_approval_detail_sql_generate()))

        sql = result.output_payload["sql"]
        self.assertEqual(result.output_payload["route_id"], "approval_variety_db")
        self.assertIn("rice_varieties.approval_num", sql)
        self.assertIn("rice_varieties.applicant", sql)
        self.assertIn("rice_varieties.breeder", sql)
        self.assertIn("rice_varieties.variety_source", sql)
        self.assertIn("rice_varieties.characteristics", sql)
        self.assertIn("rice_varieties.yield_performance", sql)
        self.assertIn("rice_varieties.cultivation_tips", sql)
        self.assertIn("rice_varieties.approval_opinion", sql)
        self.assertIn("rice_varieties.suitable_area", sql)
        self.assertIn("rice_varieties.variety_name LIKE '%龙粳18%'", sql)
        self.assertNotIn("rice_varieties.variety_name =", sql)
        self.assertNotIn("LIMIT", sql)


    def test_llm_sql_missing_required_year_constraint_falls_back(self) -> None:
        context = {
            **APPROVAL_DETAIL_SCHEMA_CONTEXT,
            "user_question": "隆平高科2021年都审定了什么品种？",
            "entities": [{"text": "隆平高科", "entity_type": "organization", "field_intent": "applicant_or_breeder"}],
        }
        context["query_constraints"] = build_query_constraints(context, current_year=2026)

        async def llm_text_generator(_: str) -> str:
            return (
                "SELECT 'rice_varieties' AS source_table, 'rice' AS source_crop, "
                "'applicant' AS matched_field, 'peer' AS match_tier, rice_varieties.year, rice_varieties.variety_name "
                "FROM rice_varieties WHERE rice_varieties.applicant LIKE '%隆平高科%' "
                "UNION ALL "
                "SELECT 'rice_varieties' AS source_table, 'rice' AS source_crop, "
                "'breeder' AS matched_field, 'peer' AS match_tier, rice_varieties.year, rice_varieties.variety_name "
                "FROM rice_varieties WHERE rice_varieties.breeder LIKE '%隆平高科%'"
            )

        result = asyncio.run(
            SQLQuerySQLGenerateCapability(llm_text_generator=llm_text_generator).execute(
                make_request("skill.sql_query", dependency_outputs={"schema": context})
            )
        )

        self.assertIsNone(result.error)
        self.assertEqual(result.output_payload["generation_source"], "fallback")
        self.assertIn("rice_varieties.year = 2021", result.output_payload["sql"])
        self.assertIn(" UNION ALL ", result.output_payload["sql"])
        self.assertNotEqual(result.output_payload["sql"].strip(), "")

    def test_llm_sql_missing_required_region_constraint_falls_back(self) -> None:
        context = {**APPROVAL_DETAIL_SCHEMA_CONTEXT, "user_question": "适合河南种植的2021年水稻品种有哪些？"}
        context["query_constraints"] = build_query_constraints(context, current_year=2026)

        async def llm_text_generator(_: str) -> str:
            return (
                "SELECT rice_varieties.year, rice_varieties.variety_name "
                "FROM rice_varieties WHERE rice_varieties.year = 2021"
            )

        result = asyncio.run(
            SQLQuerySQLGenerateCapability(llm_text_generator=llm_text_generator).execute(
                make_request("skill.sql_query", dependency_outputs={"schema": context})
            )
        )

        self.assertIsNone(result.error)
        self.assertEqual(result.output_payload["generation_source"], "fallback")
        self.assertIn("rice_varieties.year = 2021", result.output_payload["sql"])
        self.assertIn("rice_varieties.suitable_area LIKE '%河南%'", result.output_payload["sql"])
        self.assertTrue(result.output_payload["constraint_coverage_summary"]["covered"])

    def test_branch_union_constraint_rejects_untraceable_or_sql(self) -> None:
        context = {
            **APPROVAL_DETAIL_SCHEMA_CONTEXT,
            "user_question": "隆平高科都审定了什么品种？",
            "entities": [{"text": "隆平高科", "entity_type": "organization", "field_intent": "applicant_or_breeder"}],
        }
        context["query_constraints"] = build_query_constraints(context, current_year=2026)

        async def llm_text_generator(_: str) -> str:
            return (
                "SELECT rice_varieties.year, rice_varieties.variety_name "
                "FROM rice_varieties "
                "WHERE rice_varieties.applicant LIKE '%隆平高科%' OR rice_varieties.breeder LIKE '%隆平高科%'"
            )

        result = asyncio.run(
            SQLQuerySQLGenerateCapability(llm_text_generator=llm_text_generator).execute(
                make_request("skill.sql_query", dependency_outputs={"schema": context})
            )
        )

        self.assertIsNone(result.error)
        self.assertEqual(result.output_payload["generation_source"], "fallback")
        self.assertIn(" UNION ALL ", result.output_payload["sql"])
        self.assertIn("'applicant' AS matched_field", result.output_payload["sql"])
        self.assertIn("'breeder' AS matched_field", result.output_payload["sql"])

    def test_fallback_applies_query_level_order_and_limit_to_final_result(self) -> None:
        context = {**APPROVAL_DETAIL_SCHEMA_CONTEXT, "user_question": "最新审定的10个水稻品种是什么？"}
        context["query_constraints"] = build_query_constraints(context, current_year=2026)

        result = asyncio.run(
            SQLQuerySQLGenerateCapability().execute(
                make_request("skill.sql_query", dependency_outputs={"schema": context})
            )
        )

        sql = result.output_payload["sql"]
        self.assertIsNone(result.error)
        self.assertRegex(sql.lower(), r"order by year desc")
        self.assertRegex(sql.lower(), r"limit 10")
        self.assertTrue(result.output_payload["constraint_coverage_summary"]["covered"])

    def test_ast_validation_accepts_alias_and_reverse_year_comparison(self) -> None:
        context = {
            **APPROVAL_DETAIL_SCHEMA_CONTEXT,
            "user_question": "隆平高科2021年都审定了什么品种？",
            "entities": [{"text": "隆平高科", "entity_type": "organization", "field_intent": "applicant_or_breeder"}],
        }
        context["query_constraints"] = build_query_constraints(context, current_year=2026)

        async def llm_text_generator(_: str) -> str:
            return json.dumps(
                {
                    "mode": "answer",
                    "route_id": "approval_variety_db",
                    "schema_profile_id": "approval_variety_profile",
                    "sql": (
                        "SELECT 'rice_varieties' AS source_table, 'rice' AS source_crop, "
                        "'applicant' AS matched_field, 'peer' AS match_tier, rv.year, rv.variety_name "
                        "FROM rice_varieties rv WHERE 2021 = rv.year AND rv.applicant LIKE '%隆平高科%' "
                        "UNION ALL "
                        "SELECT 'rice_varieties' AS source_table, 'rice' AS source_crop, "
                        "'breeder' AS matched_field, 'peer' AS match_tier, rv.year, rv.variety_name "
                        "FROM rice_varieties rv WHERE 2021 = rv.year AND rv.breeder LIKE '%隆平高科%'"
                    ),
                    "tables_used": ["rice_varieties"],
                    "columns_used": [
                        "rice_varieties.year",
                        "rice_varieties.variety_name",
                        "rice_varieties.applicant",
                        "rice_varieties.breeder",
                    ],
                    "column_types_used": {
                        "rice_varieties.year": "int(11)",
                        "rice_varieties.variety_name": "varchar(100)",
                        "rice_varieties.applicant": "varchar(200)",
                        "rice_varieties.breeder": "varchar(200)",
                    },
                    "join_hints_used": [],
                },
                ensure_ascii=False,
            )

        result = asyncio.run(
            SQLQuerySQLGenerateCapability(llm_text_generator=llm_text_generator).execute(
                make_request("skill.sql_query", dependency_outputs={"schema": context})
            )
        )

        self.assertIsNone(result.error)
        self.assertEqual(result.output_payload["generation_source"], "llm")
        self.assertTrue(result.output_payload["constraint_coverage_summary"]["covered"])

    def test_ast_validation_accepts_cte_union_with_outer_year_filter(self) -> None:
        context = {
            **APPROVAL_DETAIL_SCHEMA_CONTEXT,
            "user_question": "2021年审定品种有哪些？",
            "selected_tables": ["rice_varieties", "wheat_varieties"],
            "allowed_tables": ["rice_varieties", "wheat_varieties"],
            "selected_columns": {
                "rice_varieties": APPROVAL_DETAIL_SCHEMA_CONTEXT["selected_columns"]["rice_varieties"],
                "wheat_varieties": APPROVAL_DETAIL_SCHEMA_CONTEXT["selected_columns"]["rice_varieties"],
            },
            "selected_column_details": {
                "rice_varieties": APPROVAL_DETAIL_SCHEMA_CONTEXT["selected_column_details"]["rice_varieties"],
                "wheat_varieties": APPROVAL_DETAIL_SCHEMA_CONTEXT["selected_column_details"]["rice_varieties"],
            },
        }
        context["query_constraints"] = build_query_constraints(context, current_year=2026)

        async def llm_text_generator(_: str) -> str:
            return json.dumps(
                {
                    "mode": "answer",
                    "route_id": "approval_variety_db",
                    "schema_profile_id": "approval_variety_profile",
                    "sql": (
                        "WITH x AS ("
                        "SELECT 'rice_varieties' AS source_table, 'rice' AS source_crop, year, variety_name FROM rice_varieties "
                        "UNION ALL "
                        "SELECT 'wheat_varieties' AS source_table, 'wheat' AS source_crop, year, variety_name FROM wheat_varieties"
                        ") SELECT * FROM x WHERE x.year = 2021"
                    ),
                    "tables_used": ["rice_varieties", "wheat_varieties"],
                    "columns_used": ["rice_varieties.year", "rice_varieties.variety_name", "wheat_varieties.year", "wheat_varieties.variety_name"],
                    "column_types_used": {
                        "rice_varieties.year": "int(11)",
                        "rice_varieties.variety_name": "varchar(100)",
                        "wheat_varieties.year": "int(11)",
                        "wheat_varieties.variety_name": "varchar(100)",
                    },
                    "join_hints_used": [],
                },
                ensure_ascii=False,
            )

        result = asyncio.run(
            SQLQuerySQLGenerateCapability(llm_text_generator=llm_text_generator).execute(
                make_request("skill.sql_query", dependency_outputs={"schema": context})
            )
        )

        self.assertIsNone(result.error)
        self.assertEqual(result.output_payload["generation_source"], "llm")
        self.assertTrue(result.output_payload["constraint_coverage_summary"]["covered"])

    def test_ast_validation_rejects_branch_local_limit_as_query_level_limit(self) -> None:
        context = {
            **APPROVAL_DETAIL_SCHEMA_CONTEXT,
            "user_question": "最新审定的10个品种是什么？",
            "selected_tables": ["rice_varieties", "wheat_varieties"],
            "allowed_tables": ["rice_varieties", "wheat_varieties"],
            "selected_columns": {
                "rice_varieties": APPROVAL_DETAIL_SCHEMA_CONTEXT["selected_columns"]["rice_varieties"],
                "wheat_varieties": APPROVAL_DETAIL_SCHEMA_CONTEXT["selected_columns"]["rice_varieties"],
            },
            "selected_column_details": {
                "rice_varieties": APPROVAL_DETAIL_SCHEMA_CONTEXT["selected_column_details"]["rice_varieties"],
                "wheat_varieties": APPROVAL_DETAIL_SCHEMA_CONTEXT["selected_column_details"]["rice_varieties"],
            },
        }
        context["query_constraints"] = build_query_constraints(context, current_year=2026)

        async def llm_text_generator(_: str) -> str:
            return json.dumps(
                {
                    "mode": "answer",
                    "route_id": "approval_variety_db",
                    "schema_profile_id": "approval_variety_profile",
                    "sql": (
                        "SELECT 'rice_varieties' AS source_table, 'rice' AS source_crop, year, variety_name "
                        "FROM rice_varieties LIMIT 10 "
                        "UNION ALL "
                        "SELECT 'wheat_varieties' AS source_table, 'wheat' AS source_crop, year, variety_name "
                        "FROM wheat_varieties"
                    ),
                    "tables_used": ["rice_varieties", "wheat_varieties"],
                    "columns_used": ["rice_varieties.year", "rice_varieties.variety_name", "wheat_varieties.year", "wheat_varieties.variety_name"],
                    "column_types_used": {
                        "rice_varieties.year": "int(11)",
                        "rice_varieties.variety_name": "varchar(100)",
                        "wheat_varieties.year": "int(11)",
                        "wheat_varieties.variety_name": "varchar(100)",
                    },
                    "join_hints_used": [],
                },
                ensure_ascii=False,
            )

        result = asyncio.run(
            SQLQuerySQLGenerateCapability(llm_text_generator=llm_text_generator).execute(
                make_request("skill.sql_query", dependency_outputs={"schema": context})
            )
        )

        self.assertIsNone(result.error)
        self.assertEqual(result.output_payload["generation_source"], "fallback")
        self.assertRegex(result.output_payload["sql"].lower(), r"order by year desc")
        self.assertRegex(result.output_payload["sql"].lower(), r"limit 10")

    def test_conflicting_year_constraints_ask_for_clarification_without_sql(self) -> None:
        context = {**APPROVAL_DETAIL_SCHEMA_CONTEXT, "user_question": "2021年和2022年都审定了什么品种？"}
        context["query_constraints"] = build_query_constraints(context, current_year=2026)

        async def llm_text_generator(_: str) -> str:
            raise AssertionError("LLM should not be called for deterministic constraint conflicts")

        result = asyncio.run(
            SQLQuerySQLGenerateCapability(llm_text_generator=llm_text_generator).execute(
                make_request("skill.sql_query", dependency_outputs={"schema": context})
            )
        )

        self.assertIsNone(result.error)
        self.assertIsNotNone(result.interrupt)
        self.assertEqual(result.output_payload["status"], "missing_input")
        self.assertEqual(result.output_payload["clarification_reason"], "conflicting_temporal_constraints")
        self.assertIn("2021", result.output_payload["answer"])
        self.assertIn("2022", result.output_payload["answer"])
        self.assertNotIn("sql", result.output_payload)

    def test_llm_clarify_returns_interrupt_without_sql(self) -> None:
        async def llm_text_generator(_: str) -> str:
            return json.dumps(
                {
                    "mode": "clarify",
                    "route_id": "genotype_db",
                    "schema_profile_id": "genotype_profile",
                    "missing_info": ["variety_name"],
                    "clarifying_question": "请补充要查询的品种名称。",
                }
            )

        capability = SQLQuerySQLGenerateCapability(llm_text_generator=llm_text_generator)

        result = asyncio.run(capability.execute(request_for_sql_generate()))

        self.assertIsNone(result.error)
        self.assertIsNotNone(result.interrupt)
        self.assertEqual(result.interrupt.question, "请补充要查询的品种名称。")
        self.assertEqual(result.interrupt.reason_code, "llm_clarification_required")
        self.assertNotIn("sql", result.output_payload)
        self.assertEqual(result.output_payload["llm_mode"], "clarify")

    def test_llm_reject_returns_non_retriable_error(self) -> None:
        async def llm_text_generator(_: str) -> str:
            return json.dumps(
                {
                    "mode": "reject",
                    "route_id": "genotype_db",
                    "schema_profile_id": "genotype_profile",
                    "reject_reason": "问题超出当前 SQLQuery 支持范围。",
                    "supported_scope_hint": "可查询品种、基因型、QTN 等信息。",
                }
            )

        capability = SQLQuerySQLGenerateCapability(llm_text_generator=llm_text_generator)

        result = asyncio.run(capability.execute(request_for_sql_generate()))

        self.assertIsNotNone(result.error)
        self.assertEqual(result.error.code, "llm_rejected_request")
        self.assertFalse(result.error.retriable)
        self.assertEqual(result.output_payload["llm_mode"], "reject")
        self.assertNotIn("sql", result.output_payload)


if __name__ == "__main__":
    unittest.main()
