from __future__ import annotations

import asyncio
import unittest

from src.capabilities.sql_query.schema_context_prepare import SQLQuerySchemaContextPrepareCapability

from tests.capabilities.sql_query.support import make_request


class SQLQuerySchemaContextPrepareTest(unittest.TestCase):
    def test_successfully_builds_schema_context_for_genotype_route(self) -> None:
        capability = SQLQuerySchemaContextPrepareCapability()
        request = make_request(
            "sql_query.schema_context_prepare",
            dependency_outputs={
                "intent": {
                    "route_id": "genotype_db",
                    "schema_profile_id": "genotype_profile",
                    "sql_policy_profile": "strict_readonly_mysql",
                    "allowed_tables": ["variety", "variety_genotype", "qtn", "rice_comp"],
                    "user_question": "查询某个品种的基因型信息",
                }
            },
        )

        result = asyncio.run(capability.execute(request))

        self.assertIsNone(result.error)
        self.assertGreaterEqual(len(result.output_payload["selected_tables"]), 1)
        self.assertIn("context_summary", result.output_payload)
        self.assertIn("selected_column_details", result.output_payload)
        for table, columns in result.output_payload["selected_column_details"].items():
            self.assertEqual(
                [column["name"] for column in columns],
                result.output_payload["selected_columns"][table],
            )
            self.assertTrue(all(column.get("sql_type") for column in columns))

    def test_builds_broad_variety_overview_context_across_approval_and_genotype_tables(self) -> None:
        capability = SQLQuerySchemaContextPrepareCapability()
        request = make_request(
            "sql_query.schema_context_prepare",
            dependency_outputs={
                "intent": {
                    "route_id": "variety_overview",
                    "schema_profile_id": "variety_overview_profile",
                    "sql_policy_profile": "strict_readonly_mysql",
                    "allowed_tables": [
                        "corn_varieties",
                        "rice_varieties",
                        "cotton_varieties",
                        "wheat_varieties",
                        "soybean_varieties",
                        "variety",
                        "rice_comp",
                    ],
                    "user_question": "查一下龙粳33",
                    "route_resolution_strategy": "first_principles_broad_variety_overview",
                }
            },
        )

        result = asyncio.run(capability.execute(request))

        self.assertIsNone(result.error)
        selected_tables = set(result.output_payload["selected_tables"])
        self.assertIn("rice_varieties", selected_tables)
        self.assertIn("variety", selected_tables)
        self.assertIn("rice_comp", selected_tables)
        self.assertIn("variety_overview", result.output_payload["metadata"]["route_notes"][0])

    def test_approval_variety_detail_context_keeps_business_detail_columns(self) -> None:
        capability = SQLQuerySchemaContextPrepareCapability()
        request = make_request(
            "sql_query.schema_context_prepare",
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
                }
            },
        )

        result = asyncio.run(capability.execute(request))

        self.assertIsNone(result.error)
        self.assertEqual(result.output_payload["selected_tables"], ["rice_varieties"])
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

    def test_route_profile_mismatch_returns_error(self) -> None:
        capability = SQLQuerySchemaContextPrepareCapability()
        request = make_request(
            "sql_query.schema_context_prepare",
            dependency_outputs={
                "intent": {
                    "route_id": "genotype_db",
                    "schema_profile_id": "approval_variety_profile",
                    "sql_policy_profile": "strict_readonly_mysql",
                    "allowed_tables": ["variety", "variety_genotype", "qtn", "rice_comp"],
                    "user_question": "查询某个品种的基因型信息",
                }
            },
        )

        result = asyncio.run(capability.execute(request))

        self.assertIsNotNone(result.error)
        self.assertEqual(result.error.code, "route_profile_mismatch")
