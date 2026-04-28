from __future__ import annotations

import asyncio
import unittest

from src.capabilities.sql_query.result_filtering import SQLQueryResultFilteringCapability

from tests.capabilities.sql_query.support import make_request


class SQLQueryResultFilteringTest(unittest.TestCase):
    def test_without_llm_keeps_table_shape_and_rows(self) -> None:
        capability = SQLQueryResultFilteringCapability()
        request = make_request(
            "sql_query.result_filtering",
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
            "sql_query.result_filtering",
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
                    "route_id": "variety_overview",
                    "schema_profile_id": "variety_overview_profile",
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
            "sql_query.result_filtering",
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


if __name__ == "__main__":
    unittest.main()
