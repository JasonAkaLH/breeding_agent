from __future__ import annotations

import _bootstrap  # noqa: F401
import asyncio
import json
import unittest

from sql_query_skill.sql_guard import SQLQuerySQLGuardCapability

from support import make_request


class SQLQuerySQLGuardTest(unittest.TestCase):
    def test_safe_select_without_limit_passes(self) -> None:
        capability = SQLQuerySQLGuardCapability()
        request = make_request(
            "skill.sql_query",
            dependency_outputs={
                "sql_generate": {
                    "route_id": "genotype_db",
                    "schema_profile_id": "genotype_profile",
                    "allowed_tables": ["variety", "variety_genotype", "qtn", "rice_comp"],
                    "selected_tables": ["variety"],
                    "sql": "SELECT variety_name FROM variety",
                }
            },
        )

        result = asyncio.run(capability.execute(request))

        self.assertIsNone(result.error)
        self.assertIn("guard_pass_token", result.output_payload)
        artifact_payload = json.loads(result.artifacts[0].storage_ref)
        self.assertNotIn("guard_pass_token", artifact_payload)

    def test_insert_is_blocked(self) -> None:
        capability = SQLQuerySQLGuardCapability()
        request = make_request(
            "skill.sql_query",
            dependency_outputs={
                "sql_generate": {
                    "route_id": "genotype_db",
                    "schema_profile_id": "genotype_profile",
                    "allowed_tables": ["variety", "variety_genotype", "qtn", "rice_comp"],
                    "selected_tables": ["variety"],
                    "sql": "INSERT INTO variety(variety_name) VALUES ('x')",
                }
            },
        )

        result = asyncio.run(capability.execute(request))

        self.assertIsNotNone(result.error)
        self.assertEqual(result.error.code, "statement_root_denied")

    def test_with_select_is_allowed_when_real_table_is_selected(self) -> None:
        capability = SQLQuerySQLGuardCapability()
        request = make_request(
            "skill.sql_query",
            dependency_outputs={
                "sql_generate": {
                    "route_id": "approval_variety_db",
                    "schema_profile_id": "approval_variety_profile",
                    "selected_tables": ["rice_varieties"],
                    "sql": "WITH x AS (SELECT variety_name FROM rice_varieties) SELECT * FROM x",
                }
            },
        )

        result = asyncio.run(capability.execute(request))

        self.assertIsNone(result.error)
        self.assertIn("guard_pass_token", result.output_payload)

    def test_union_all_select_is_allowed(self) -> None:
        capability = SQLQuerySQLGuardCapability()
        request = make_request(
            "skill.sql_query",
            dependency_outputs={
                "sql_generate": {
                    "route_id": "approval_variety_db",
                    "schema_profile_id": "approval_variety_profile",
                    "selected_tables": ["rice_varieties", "wheat_varieties"],
                    "sql": (
                        "SELECT variety_name FROM rice_varieties "
                        "UNION ALL SELECT variety_name FROM wheat_varieties"
                    ),
                }
            },
        )

        result = asyncio.run(capability.execute(request))

        self.assertIsNone(result.error)
        self.assertIn("guard_pass_token", result.output_payload)

    def test_parse_failure_is_blocked(self) -> None:
        capability = SQLQuerySQLGuardCapability()
        request = make_request(
            "skill.sql_query",
            dependency_outputs={
                "sql_generate": {
                    "route_id": "approval_variety_db",
                    "schema_profile_id": "approval_variety_profile",
                    "selected_tables": ["rice_varieties"],
                    "sql": "SELECT FROM",
                }
            },
        )

        result = asyncio.run(capability.execute(request))

        self.assertIsNotNone(result.error)
        self.assertEqual(result.error.code, "sql_parse_failed")

    def test_large_limit_is_not_capped_by_guard(self) -> None:
        capability = SQLQuerySQLGuardCapability()
        request = make_request(
            "skill.sql_query",
            dependency_outputs={
                "sql_generate": {
                    "route_id": "genotype_db",
                    "schema_profile_id": "genotype_profile",
                    "allowed_tables": ["variety", "variety_genotype", "qtn", "rice_comp"],
                    "selected_tables": ["variety"],
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
            "skill.sql_query",
            dependency_outputs={
                "sql_generate": {
                    "route_id": "genotype_db",
                    "schema_profile_id": "genotype_profile",
                    "allowed_tables": ["variety", "variety_genotype", "qtn", "rice_comp"],
                    "selected_tables": ["variety"],
                    "sql": "SELECT table_name FROM information_schema.tables LIMIT 10",
                }
            },
        )

        result = asyncio.run(capability.execute(request))

        self.assertIsNotNone(result.error)
        self.assertEqual(result.error.code, "system_schema_access_denied")

    def test_selected_tables_are_required_even_when_allowed_tables_exist(self) -> None:
        capability = SQLQuerySQLGuardCapability()
        request = make_request(
            "skill.sql_query",
            dependency_outputs={
                "sql_generate": {
                    "route_id": "approval_variety_db",
                    "schema_profile_id": "approval_variety_profile",
                    "allowed_tables": ["corn_varieties", "rice_varieties"],
                    "selected_tables": [],
                    "sql": "SELECT variety_name FROM rice_varieties",
                }
            },
        )

        result = asyncio.run(capability.execute(request))

        self.assertIsNotNone(result.error)
        self.assertEqual(result.error.code, "selected_tables_required")

    def test_selected_tables_narrow_route_whitelist(self) -> None:
        capability = SQLQuerySQLGuardCapability()
        request = make_request(
            "skill.sql_query",
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

    def test_multiline_select_root_passes_guard(self) -> None:
        capability = SQLQuerySQLGuardCapability()
        request = make_request(
            "skill.sql_query",
            dependency_outputs={
                "sql_generate": {
                    "route_id": "approval_variety_db",
                    "schema_profile_id": "approval_variety_profile",
                    "allowed_tables": ["corn_varieties"],
                    "selected_tables": ["corn_varieties"],
                    "sql": """
                    SELECT
                        year,
                        variety_name
                    FROM corn_varieties
                    WHERE year = 2021
                    """,
                }
            },
        )

        result = asyncio.run(capability.execute(request))

        self.assertIsNone(result.error)
        self.assertEqual(
            result.output_payload["sql"],
            "SELECT year, variety_name FROM corn_varieties WHERE year = 2021",
        )
        self.assertIn("guard_pass_token", result.output_payload)

    def test_guard_remains_safety_boundary_not_full_parser(self) -> None:
        capability = SQLQuerySQLGuardCapability()
        request = make_request(
            "skill.sql_query",
            dependency_outputs={
                "sql_generate": {
                    "route_id": "approval_variety_db",
                    "schema_profile_id": "approval_variety_profile",
                    "allowed_tables": ["corn_varieties"],
                    "selected_tables": ["corn_varieties"],
                    "sql": "SELECT COALESCE(applicant, breeder) AS owner_name FROM corn_varieties WHERE year + 1 > 2021",
                }
            },
        )

        result = asyncio.run(capability.execute(request))

        self.assertIsNone(result.error)
        self.assertIn("guard_pass_token", result.output_payload)
