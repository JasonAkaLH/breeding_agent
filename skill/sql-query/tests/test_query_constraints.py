from __future__ import annotations

import _bootstrap  # noqa: F401
import unittest

from sql_query_skill.query_constraints import build_query_constraints, validate_structured_extractor_output


BASE_CONTEXT = {
    "route_id": "approval_variety_db",
    "schema_profile_id": "approval_variety_profile",
    "selected_tables": ["rice_varieties"],
    "selected_columns": {
        "rice_varieties": [
            "year",
            "approval_num",
            "crop_name",
            "variety_name",
            "applicant",
            "breeder",
            "suitable_area",
        ]
    },
}


def constraints_by_field(contract: dict) -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = {}
    for item in contract["required_constraints"]:
        result.setdefault(item["field"], []).append(item)
    return result


class QueryConstraintsTest(unittest.TestCase):
    def test_extracts_year_and_peer_applicant_breeder_branch_union(self) -> None:
        contract = build_query_constraints(
            {**BASE_CONTEXT, "user_question": "隆平高科2021年都审定了什么品种？"},
            current_year=2026,
        )

        by_field = constraints_by_field(contract)
        self.assertEqual(by_field["year"][0]["operator"], "=")
        self.assertEqual(by_field["year"][0]["value"], 2021)
        self.assertEqual(by_field["year"][0]["scope"], "global_filter")
        self.assertEqual(by_field["applicant"][0]["value"], "隆平高科")
        self.assertEqual(by_field["breeder"][0]["value"], "隆平高科")
        self.assertEqual(by_field["applicant"][0]["match_tier"], "peer")
        self.assertEqual(by_field["breeder"][0]["match_tier"], "peer")
        self.assertEqual(contract["constraint_groups"][0]["mode"], "branch_union")
        self.assertEqual(set(contract["constraint_groups"][0]["members"]), {by_field["applicant"][0]["id"], by_field["breeder"][0]["id"]})

    def test_recent_year_uses_freeze_clock_and_explicit_applicant_primary(self) -> None:
        contract = build_query_constraints(
            {**BASE_CONTEXT, "user_question": "近五年隆平高科申请审定了哪些品种？"},
            current_year=2026,
        )

        by_field = constraints_by_field(contract)
        self.assertEqual(by_field["year"][0]["operator"], ">=")
        self.assertEqual(by_field["year"][0]["value"], 2022)
        self.assertEqual(by_field["applicant"][0]["match_tier"], "primary")
        self.assertEqual(by_field["breeder"][0]["match_tier"], "secondary")

    def test_extracts_region_and_year_without_expanding_table_scope(self) -> None:
        contract = build_query_constraints(
            {**BASE_CONTEXT, "user_question": "适合河南种植的2021年水稻品种有哪些？"},
            current_year=2026,
        )

        by_field = constraints_by_field(contract)
        self.assertEqual(by_field["year"][0]["value"], 2021)
        self.assertEqual(by_field["suitable_area"][0]["operator"], "LIKE")
        self.assertEqual(by_field["suitable_area"][0]["value"], "河南")
        self.assertEqual(by_field["suitable_area"][0]["tables"], ["rice_varieties"])

    def test_extracts_approval_number(self) -> None:
        contract = build_query_constraints(
            {**BASE_CONTEXT, "user_question": "国审稻20210001是什么品种？"},
            current_year=2026,
        )

        approval = constraints_by_field(contract)["approval_num"][0]
        self.assertEqual(approval["operator"], "LIKE")
        self.assertEqual(approval["value"], "国审稻20210001")
        self.assertTrue(approval["required"])
        self.assertEqual(approval["confidence"], "high")

    def test_conflicting_years_trigger_clarification_and_do_not_extract_connector_entity(self) -> None:
        contract = build_query_constraints(
            {**BASE_CONTEXT, "user_question": "2021年和2022年都审定了什么品种？"},
            current_year=2026,
        )

        by_field = constraints_by_field(contract)
        self.assertNotIn("year", by_field)
        self.assertNotIn("applicant", by_field)
        self.assertNotIn("breeder", by_field)
        self.assertEqual(contract["clarification_needed"]["reason"], "conflicting_temporal_constraints")
        self.assertEqual(contract["clarification_needed"]["candidate_years"], [2021, 2022])
        self.assertEqual(
            [item["value"] for item in contract["soft_constraints"] if item.get("field") == "year"],
            [2021, 2022],
        )

    def test_invalid_structured_llm_suggestions_are_discarded(self) -> None:
        validated = validate_structured_extractor_output(
            {
                "suggested_constraints": [
                    {"kind": "entity", "field": "unknown_field", "operator": "LIKE", "value": "x", "confidence": "high", "source_span": "x"},
                    {"kind": "entity", "field": "applicant", "operator": "=~", "value": "x", "confidence": "high", "source_span": "x"},
                    {"kind": "entity", "field": "applicant", "operator": "LIKE", "value": "x", "confidence": "low", "source_span": "x"},
                    {"kind": "entity", "field": "applicant", "operator": "LIKE", "value": "x", "confidence": "high"},
                ]
            },
            selected_columns=BASE_CONTEXT["selected_columns"],
        )

        self.assertEqual(validated["suggested_constraints"], [])
        self.assertEqual(len(validated["discarded"]), 4)
        contract = build_query_constraints(
            {**BASE_CONTEXT, "user_question": "隆平高科2021年都审定了什么品种？"},
            current_year=2026,
            structured_llm_output={"suggested_constraints": [{"field": "unknown_field", "operator": "LIKE", "value": "x", "confidence": "high", "source_span": "x"}]},
        )
        self.assertIn("year", constraints_by_field(contract))
        self.assertNotIn("unknown_field", constraints_by_field(contract))


if __name__ == "__main__":
    unittest.main()
