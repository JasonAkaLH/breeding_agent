from __future__ import annotations

import _bootstrap  # noqa: F401
import asyncio
import unittest

from sql_query_skill.schema_context_prepare import SQLQuerySchemaContextPrepareCapability

from support import make_request


class SQLQuerySchemaContextPrepareTest(unittest.TestCase):
    def test_successfully_builds_schema_context_for_genotype_route(self) -> None:
        capability = SQLQuerySchemaContextPrepareCapability()
        request = make_request(
            "skill.sql_query",
            dependency_outputs={
                "intent": {
                    "route_id": "genotype_db",
                    "schema_profile_id": "genotype_profile",
                    "sql_policy_profile": "strict_readonly_mysql",
                    "allowed_tables": ["variety", "variety_genotype", "qtn", "rice_comp"],
                    "selected_tables": ["variety", "variety_genotype", "qtn", "rice_comp"],
                    "user_question": "查询某个品种的基因型信息",
                }
            },
        )

        result = asyncio.run(capability.execute(request))

        self.assertIsNone(result.error)
        self.assertGreaterEqual(len(result.output_payload["selected_tables"]), 1)
        self.assertIn("context_summary", result.output_payload)
        self.assertIn("selected_column_details", result.output_payload)
        self.assertIn("schema_ddl", result.output_payload)
        self.assertIn("CREATE TABLE `variety`", result.output_payload["schema_ddl"])
        self.assertIn("表结构：variety", result.output_payload["schema_ddl"])
        self.assertIn("COMMENT '品种名称'", result.output_payload["schema_ddl"])
        self.assertEqual(
            set(result.output_payload["selected_tables"]),
            {"variety", "variety_genotype", "qtn", "rice_comp"},
        )
        for table, columns in result.output_payload["selected_column_details"].items():
            self.assertEqual(
                [column["name"] for column in columns],
                result.output_payload["selected_columns"][table],
            )
            self.assertTrue(all(column.get("sql_type") for column in columns))

    def test_approval_variety_detail_context_keeps_business_detail_columns(self) -> None:
        capability = SQLQuerySchemaContextPrepareCapability()
        request = make_request(
            "skill.sql_query",
            dependency_outputs={
                "intent": {
                    "route_id": "approval_variety_db",
                    "schema_profile_id": "approval_variety_profile",
                    "sql_policy_profile": "strict_readonly_mysql",
                    "allowed_tables": [
                        "corn_varieties",
                        "rice_varieties",
                        "cotton_varieties",
                        "wheat_varieties",
                        "soybean_varieties",
                    ],
                    "user_question": "龙粳18的详细审定信息，要所有信息",
                    "inferred_crop": "rice",
                    "selected_tables": ["rice_varieties"],
                }
            },
        )

        result = asyncio.run(capability.execute(request))

        self.assertIsNone(result.error)
        self.assertEqual(result.output_payload["selected_tables"], ["rice_varieties"])
        self.assertIn("CREATE TABLE `rice_varieties`", result.output_payload["schema_ddl"])
        self.assertNotIn("CREATE TABLE `corn_varieties`", result.output_payload["schema_ddl"])
        selected = result.output_payload["selected_columns"]["rice_varieties"]
        for column in [
            "variety_name",
            "approval_num",
            "year",
            "applicant",
            "breeder",
            "variety_source",
            "characteristics",
            "yield_performance",
            "cultivation_tips",
            "approval_opinion",
            "suitable_area",
        ]:
            self.assertIn(column, selected)

    def test_approval_without_crop_builds_broad_context_across_all_approval_tables(self) -> None:
        capability = SQLQuerySchemaContextPrepareCapability()
        request = make_request(
            "skill.sql_query",
            dependency_outputs={
                "intent": {
                    "route_id": "approval_variety_db",
                    "schema_profile_id": "approval_variety_profile",
                    "sql_policy_profile": "strict_readonly_mysql",
                    "allowed_tables": [
                        "corn_varieties",
                        "rice_varieties",
                        "cotton_varieties",
                        "wheat_varieties",
                        "soybean_varieties",
                    ],
                    "user_question": "查询近五年审定品种有哪些",
                    "inferred_crop": None,
                    "selected_tables": [
                        "corn_varieties",
                        "rice_varieties",
                        "cotton_varieties",
                        "wheat_varieties",
                        "soybean_varieties",
                    ],
                    "no_crop_broad_query": True,
                    "route_resolution_strategy": "no_crop_approval_broad",
                }
            },
        )

        result = asyncio.run(capability.execute(request))

        self.assertIsNone(result.error)
        self.assertIsNone(result.interrupt)
        self.assertEqual(
            set(result.output_payload["selected_tables"]),
            {"corn_varieties", "rice_varieties", "cotton_varieties", "wheat_varieties", "soybean_varieties"},
        )
        metadata = result.output_payload["metadata"]
        self.assertTrue(metadata["no_crop_broad_query"])
        self.assertIn("all approval crop tables", metadata["route_notes"][0])

    def test_approval_schema_exposes_all_llm_visible_columns_for_llm_selection(self) -> None:
        capability = SQLQuerySchemaContextPrepareCapability()
        request = make_request(
            "skill.sql_query",
            dependency_outputs={
                "intent": {
                    "route_id": "approval_variety_db",
                    "schema_profile_id": "approval_variety_profile",
                    "sql_policy_profile": "strict_readonly_mysql",
                    "allowed_tables": [
                        "corn_varieties",
                        "rice_varieties",
                        "cotton_varieties",
                        "wheat_varieties",
                        "soybean_varieties",
                    ],
                    "user_question": "给我查一下适合河南种植的水稻",
                    "inferred_crop": "rice",
                    "selected_tables": ["rice_varieties"],
                }
            },
        )

        result = asyncio.run(capability.execute(request))

        self.assertIsNone(result.error)
        selected = result.output_payload["selected_columns"]["rice_varieties"]
        self.assertIn("suitable_area", selected)
        self.assertIn("plant_height", selected)
        self.assertIn("cold_resistance_shell_rate", selected)
        self.assertNotIn("id", selected)
        self.assertEqual(
            result.output_payload["metadata"]["column_selection_strategy"],
            "llm_visible_all_exposed_columns",
        )


    def test_approval_schema_context_attaches_query_constraints(self) -> None:
        capability = SQLQuerySchemaContextPrepareCapability()
        request = make_request(
            "skill.sql_query",
            dependency_outputs={
                "intent": {
                    "route_id": "approval_variety_db",
                    "schema_profile_id": "approval_variety_profile",
                    "sql_policy_profile": "strict_readonly_mysql",
                    "allowed_tables": ["rice_varieties"],
                    "user_question": "隆平高科2021年都审定了什么品种？",
                    "selected_tables": ["rice_varieties"],
                    "entities": [{"text": "隆平高科", "entity_type": "organization", "field_intent": "applicant_or_breeder"}],
                }
            },
            metadata={"current_year": 2026},
        )

        result = asyncio.run(capability.execute(request))

        self.assertIsNone(result.error)
        constraints = result.output_payload["query_constraints"]
        by_field = {item["field"]: item for item in constraints["required_constraints"]}
        self.assertEqual(by_field["year"]["value"], 2021)
        self.assertEqual(by_field["year"]["tables"], ["rice_varieties"])
        self.assertEqual(by_field["applicant"]["tables"], ["rice_varieties"])
        self.assertEqual(by_field["breeder"]["tables"], ["rice_varieties"])
        self.assertEqual(constraints["constraint_groups"][0]["mode"], "branch_union")
        self.assertEqual(result.output_payload["selected_tables"], ["rice_varieties"])
        self.assertIn("CREATE TABLE `rice_varieties`", result.output_payload["schema_ddl"])

    def test_route_profile_mismatch_returns_error(self) -> None:
        capability = SQLQuerySchemaContextPrepareCapability()
        request = make_request(
            "skill.sql_query",
            dependency_outputs={
                "intent": {
                    "route_id": "genotype_db",
                    "schema_profile_id": "approval_variety_profile",
                    "sql_policy_profile": "strict_readonly_mysql",
                    "allowed_tables": ["variety", "variety_genotype", "qtn", "rice_comp"],
                    "selected_tables": ["variety", "variety_genotype", "qtn", "rice_comp"],
                    "user_question": "查询某个品种的基因型信息",
                }
            },
        )

        result = asyncio.run(capability.execute(request))

        self.assertIsNotNone(result.error)
        self.assertEqual(result.error.code, "route_profile_mismatch")
