from __future__ import annotations

import asyncio
import unittest

from src.capabilities.sql_query.sql_guard import SQLQuerySQLGuardCapability

from tests.capabilities.sql_query.support import make_request


class SQLQuerySQLGuardTest(unittest.TestCase):
    def test_safe_select_without_limit_passes(self) -> None:
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

    def test_large_limit_is_not_capped_by_guard(self) -> None:
        capability = SQLQuerySQLGuardCapability()
        request = make_request(
            "sql_query.sql_guard",
            dependency_outputs={
                "sql_generate": {
                    "route_id": "genotype_db",
                    "schema_profile_id": "genotype_profile",
                    "allowed_tables": ["variety", "variety_genotype", "qtn", "rice_comp"],
                    "sql": "SELECT variety_name FROM variety LIMIT 1000",
                }
            },
        )

        result = asyncio.run(capability.execute(request))

        self.assertIsNone(result.error)
        self.assertIn("guard_pass_token", result.output_payload)

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

    def test_selected_tables_narrow_route_whitelist(self) -> None:
        capability = SQLQuerySQLGuardCapability()
        request = make_request(
            "sql_query.sql_guard",
            dependency_outputs={
                "sql_generate": {
                    "route_id": "approval_variety_db",
                    "schema_profile_id": "approval_variety_profile",
                    "allowed_tables": ["corn_varieties", "rice_varieties"],
                    "selected_tables": ["rice_varieties"],
                    "sql": "SELECT variety_name FROM corn_varieties",
                }
            },
        )

        result = asyncio.run(capability.execute(request))

        self.assertIsNotNone(result.error)
        self.assertEqual(result.error.code, "table_not_in_route_whitelist")
