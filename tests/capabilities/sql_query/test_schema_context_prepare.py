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
