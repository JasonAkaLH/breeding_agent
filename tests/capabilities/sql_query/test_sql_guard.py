from __future__ import annotations

import asyncio
import unittest

from src.capabilities.sql_query.sql_guard import SQLQuerySQLGuardCapability

from tests.capabilities.sql_query.support import make_request


class SQLQuerySQLGuardTest(unittest.TestCase):
    def test_safe_select_with_limit_passes(self) -> None:
        capability = SQLQuerySQLGuardCapability()
        request = make_request(
            "sql_query.sql_guard",
            dependency_outputs={
                "sql_generate": {
                    "route_id": "genotype_db",
                    "schema_profile_id": "genotype_profile",
                    "allowed_tables": ["variety", "variety_genotype", "qtn", "rice_comp"],
                    "sql": "SELECT variety_name FROM variety LIMIT 20",
                }
            },
        )

        result = asyncio.run(capability.execute(request))

        self.assertIsNone(result.error)
        self.assertIn("guard_pass_token", result.output_payload)

    def test_insert_is_blocked(self) -> None:
        capability = SQLQuerySQLGuardCapability()
        request = make_request(
            "sql_query.sql_guard",
            dependency_outputs={
                "sql_generate": {
                    "route_id": "genotype_db",
                    "schema_profile_id": "genotype_profile",
                    "allowed_tables": ["variety", "variety_genotype", "qtn", "rice_comp"],
                    "sql": "INSERT INTO variety(variety_name) VALUES ('x')",
                }
            },
        )

        result = asyncio.run(capability.execute(request))

        self.assertIsNotNone(result.error)
        self.assertEqual(result.error.code, "statement_root_denied")

    def test_missing_limit_is_blocked(self) -> None:
        capability = SQLQuerySQLGuardCapability()
        request = make_request(
            "sql_query.sql_guard",
            dependency_outputs={
                "sql_generate": {
                    "route_id": "genotype_db",
                    "schema_profile_id": "genotype_profile",
                    "allowed_tables": ["variety", "variety_genotype", "qtn", "rice_comp"],
                    "sql": "SELECT variety_name FROM variety",
                }
            },
        )

        result = asyncio.run(capability.execute(request))

        self.assertIsNotNone(result.error)
        self.assertEqual(result.error.code, "limit_missing")

    def test_system_schema_access_is_blocked(self) -> None:
        capability = SQLQuerySQLGuardCapability()
        request = make_request(
            "sql_query.sql_guard",
            dependency_outputs={
                "sql_generate": {
                    "route_id": "genotype_db",
                    "schema_profile_id": "genotype_profile",
                    "allowed_tables": ["variety", "variety_genotype", "qtn", "rice_comp"],
                    "sql": "SELECT table_name FROM information_schema.tables LIMIT 10",
                }
            },
        )

        result = asyncio.run(capability.execute(request))

        self.assertIsNotNone(result.error)
        self.assertEqual(result.error.code, "system_schema_access_denied")
