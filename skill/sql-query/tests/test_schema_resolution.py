from __future__ import annotations

import _bootstrap  # noqa: F401
import asyncio
import unittest

from src.integrations.mysql_readonly import ReadonlyQueryResult
from sql_query_skill.schema_resolution import SQLQuerySchemaResolutionCapability

from support import make_request


BASE_APPROVAL = {
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
    "user_question": "查询龙粳18的审定信息",
    "original_user_query": "查询龙粳18的审定信息",
    "resolved_user_query": "查询龙粳18的审定信息",
}


class SQLQuerySchemaResolutionTest(unittest.TestCase):
    def test_genotype_route_selects_fixed_tables(self) -> None:
        capability = SQLQuerySchemaResolutionCapability(adapter=object())  # type: ignore[arg-type]
        request = make_request(
            "skill.sql_query",
            dependency_outputs={
                "intent": {
                    "route_id": "genotype_db",
                    "schema_profile_id": "genotype_profile",
                    "sql_policy_profile": "strict_readonly_mysql",
                    "allowed_tables": ["variety", "variety_genotype", "qtn", "rice_comp"],
                    "user_question": "查询龙粳33的基因型信息",
                }
            },
        )

        result = asyncio.run(capability.execute(request))

        self.assertIsNone(result.interrupt)
        self.assertEqual(result.output_payload["selected_tables"], ["variety", "variety_genotype", "qtn", "rice_comp"])
        self.assertEqual(result.output_payload["resolution_reason"], "genotype_fixed_tables")

    def test_approval_crop_maps_to_single_species_table(self) -> None:
        capability = SQLQuerySchemaResolutionCapability(adapter=object())  # type: ignore[arg-type]
        request = make_request(
            "skill.sql_query",
            dependency_outputs={"intent": {**BASE_APPROVAL, "inferred_crop": "rice"}},
        )

        result = asyncio.run(capability.execute(request))

        self.assertIsNone(result.interrupt)
        self.assertEqual(result.output_payload["selected_tables"], ["rice_varieties"])
        self.assertEqual(result.output_payload["selected_crops"], ["rice"])
        self.assertEqual(result.output_payload["resolution_reason"], "approval_crop_mapping")

    def test_missing_crop_with_named_variety_probes_all_species_without_limit_and_uses_single_hit(self) -> None:
        calls: list[tuple[str, str | None]] = []
        test_case = self

        class ProbeAdapter:
            async def execute_readonly(self, sql: str, *, guard_pass_token: str | None, row_retention: str = "tail"):
                calls.append((sql, row_retention))
                test_case.assertNotIn(" LIMIT ", f" {sql.upper()} ")
                if "FROM rice_varieties" in sql:
                    return ReadonlyQueryResult(
                        columns=("source_table", "source_crop", "crop_name", "variety_name", "approval_num", "year"),
                        rows=(({
                            "source_table": "rice_varieties",
                            "source_crop": "rice",
                            "crop_name": "水稻",
                            "variety_name": "龙粳18",
                            "approval_num": "黑审稻",
                            "year": 2020,
                        }),),
                        row_count=1,
                        source_row_count=1,
                    )
                return ReadonlyQueryResult(columns=(), rows=(), row_count=0, source_row_count=0)

        capability = SQLQuerySchemaResolutionCapability(adapter=ProbeAdapter())  # type: ignore[arg-type]
        request = make_request(
            "skill.sql_query",
            dependency_outputs={"intent": {**BASE_APPROVAL, "variety_name_candidates": ["龙粳18"]}},
        )

        result = asyncio.run(capability.execute(request))

        self.assertIsNone(result.interrupt)
        self.assertEqual(result.output_payload["selected_tables"], ["rice_varieties"])
        self.assertEqual(result.output_payload["resolution_reason"], "approval_entity_probe_hits")
        self.assertEqual(result.output_payload["matched_fields"], ["variety_name"])
        self.assertEqual(result.output_payload["match_summary"]["primary"][0]["field"], "variety_name")
        self.assertGreaterEqual(len(calls), 5)
        self.assertTrue(all(retention == "head" for _sql, retention in calls))

    def test_multiple_probe_hits_select_all_hit_tables_without_interrupt(self) -> None:
        class ProbeAdapter:
            async def execute_readonly(self, sql: str, *, guard_pass_token: str | None, row_retention: str = "tail"):
                if "FROM rice_varieties" in sql or "FROM corn_varieties" in sql:
                    table = "rice_varieties" if "FROM rice_varieties" in sql else "corn_varieties"
                    crop = "rice" if table == "rice_varieties" else "corn"
                    return ReadonlyQueryResult(
                        columns=("source_table", "source_crop", "crop_name", "variety_name", "approval_num", "year"),
                        rows=({"source_table": table, "source_crop": crop, "variety_name": "测试18", "approval_num": "审定号", "year": 2020},),
                        row_count=1,
                        source_row_count=1,
                    )
                return ReadonlyQueryResult(columns=(), rows=(), row_count=0, source_row_count=0)

        capability = SQLQuerySchemaResolutionCapability(adapter=ProbeAdapter())  # type: ignore[arg-type]
        request = make_request(
            "skill.sql_query",
            dependency_outputs={"intent": {**BASE_APPROVAL, "user_question": "查询测试18", "variety_name_candidates": ["测试18"]}},
        )

        result = asyncio.run(capability.execute(request))

        self.assertIsNone(result.interrupt)
        self.assertEqual(result.output_payload["resolution_reason"], "approval_entity_probe_hits")
        self.assertEqual(set(result.output_payload["selected_tables"]), {"rice_varieties", "corn_varieties"})
        self.assertEqual(result.output_payload["matched_fields"], ["variety_name"])

    def test_probe_no_hits_reports_effort_before_asking_for_crop(self) -> None:
        class EmptyAdapter:
            async def execute_readonly(self, sql: str, *, guard_pass_token: str | None, row_retention: str = "tail"):
                return ReadonlyQueryResult(columns=(), rows=(), row_count=0, source_row_count=0)

        capability = SQLQuerySchemaResolutionCapability(adapter=EmptyAdapter())  # type: ignore[arg-type]
        request = make_request(
            "skill.sql_query",
            dependency_outputs={"intent": {**BASE_APPROVAL, "variety_name_candidates": ["不存在18"]}},
        )

        result = asyncio.run(capability.execute(request))

        self.assertIsNotNone(result.interrupt)
        self.assertEqual(result.interrupt.reason_code, "approval_entity_probe_no_hits")
        self.assertIn("五个审定品种表", result.interrupt.question)
        self.assertIn("没有找到匹配记录", result.interrupt.question)
        self.assertIn("品种名字段", result.interrupt.question)

    def test_explicit_applicant_probe_uses_primary_and_secondary_fields(self) -> None:
        calls: list[str] = []

        class ProbeAdapter:
            async def execute_readonly(self, sql: str, *, guard_pass_token: str | None, row_retention: str = "tail"):
                calls.append(sql)
                if "FROM corn_varieties" in sql and "applicant LIKE" in sql:
                    return ReadonlyQueryResult(
                        columns=("source_table", "source_crop", "matched_field", "match_tier", "variety_name", "applicant", "breeder"),
                        rows=({"source_table": "corn_varieties", "source_crop": "corn", "matched_field": "applicant", "match_tier": "primary", "variety_name": "测试玉米", "applicant": "隆平高科", "breeder": ""},),
                        row_count=1,
                        source_row_count=1,
                    )
                if "FROM rice_varieties" in sql and "breeder LIKE" in sql:
                    return ReadonlyQueryResult(
                        columns=("source_table", "source_crop", "matched_field", "match_tier", "variety_name", "applicant", "breeder"),
                        rows=({"source_table": "rice_varieties", "source_crop": "rice", "matched_field": "breeder", "match_tier": "secondary", "variety_name": "测试水稻", "applicant": "", "breeder": "隆平高科"},),
                        row_count=1,
                        source_row_count=1,
                    )
                return ReadonlyQueryResult(columns=(), rows=(), row_count=0, source_row_count=0)

        capability = SQLQuerySchemaResolutionCapability(adapter=ProbeAdapter())  # type: ignore[arg-type]
        request = make_request(
            "skill.sql_query",
            dependency_outputs={
                "intent": {
                    **BASE_APPROVAL,
                    "user_question": "隆平高科申请的审定品种",
                    "entities": [
                        {
                            "text": "隆平高科",
                            "entity_type": "organization",
                            "field_intent": "applicant",
                        }
                    ],
                }
            },
        )

        result = asyncio.run(capability.execute(request))

        self.assertIsNone(result.interrupt)
        self.assertEqual(set(result.output_payload["selected_tables"]), {"corn_varieties", "rice_varieties"})
        self.assertEqual(result.output_payload["matched_fields"], ["applicant", "breeder"])
        self.assertEqual(result.output_payload["match_summary"]["primary"][0]["field"], "applicant")
        self.assertEqual(result.output_payload["match_summary"]["secondary"][0]["field"], "breeder")
        self.assertTrue(any("applicant LIKE" in sql for sql in calls))
        self.assertTrue(any("breeder LIKE" in sql for sql in calls))

    def test_uncertain_entity_uses_core_peer_fields(self) -> None:
        calls: list[str] = []

        class EmptyAdapter:
            async def execute_readonly(self, sql: str, *, guard_pass_token: str | None, row_retention: str = "tail"):
                calls.append(sql)
                return ReadonlyQueryResult(columns=(), rows=(), row_count=0, source_row_count=0)

        capability = SQLQuerySchemaResolutionCapability(adapter=EmptyAdapter())  # type: ignore[arg-type]
        request = make_request(
            "skill.sql_query",
            dependency_outputs={
                "intent": {
                    **BASE_APPROVAL,
                    "user_question": "查询“隆平高科”的审定品种",
                    "entities": [{"text": "隆平高科", "entity_type": "other", "field_intent": "unknown"}],
                }
            },
        )

        result = asyncio.run(capability.execute(request))

        self.assertIsNotNone(result.interrupt)
        self.assertEqual(
            set(result.output_payload["probe_summary"]["searched_fields"]),
            {"variety_name", "applicant", "breeder"},
        )
        self.assertTrue(any("variety_name LIKE" in sql for sql in calls))
        self.assertTrue(any("applicant LIKE" in sql for sql in calls))
        self.assertTrue(any("breeder LIKE" in sql for sql in calls))

    def test_transgenic_owner_query_is_excluded_from_entity_probe(self) -> None:
        capability = SQLQuerySchemaResolutionCapability(adapter=object())  # type: ignore[arg-type]
        request = make_request(
            "skill.sql_query",
            dependency_outputs={"intent": {**BASE_APPROVAL, "user_question": "查询隆平高科作为转化体所有者的审定品种"}},
        )

        result = asyncio.run(capability.execute(request))

        self.assertIsNotNone(result.interrupt)
        self.assertEqual(result.interrupt.reason_code, "unsupported_entity_field")
        self.assertIn("不支持按转化体所有者字段查询", result.interrupt.question)


if __name__ == "__main__":
    unittest.main()
