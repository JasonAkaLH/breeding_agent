from __future__ import annotations

import asyncio
import unittest

from src.capabilities.sql_query.intent_route import SQLQueryIntentRouteCapability

from tests.capabilities.sql_query.support import make_request


class SQLQueryIntentRouteTest(unittest.TestCase):
    def test_genotype_question_routes_to_genotype_db(self) -> None:
        capability = SQLQueryIntentRouteCapability()
        request = make_request("sql_query.intent_route", input_payload={"user_question": "查询某个品种的基因型信息"})

        result = asyncio.run(capability.execute(request))

        self.assertIsNone(result.interrupt)
        self.assertEqual(result.output_payload["route_id"], "genotype_db")
        self.assertEqual(result.output_payload["schema_profile_id"], "genotype_profile")

    def test_broad_variety_question_routes_to_overview_without_clarification(self) -> None:
        capability = SQLQueryIntentRouteCapability()
        request = make_request("sql_query.intent_route", input_payload={"user_question": "查一下龙粳33"})

        result = asyncio.run(capability.execute(request))

        self.assertIsNone(result.interrupt)
        self.assertEqual(result.output_payload["route_id"], "variety_overview")
        self.assertEqual(result.output_payload["schema_profile_id"], "variety_overview_profile")
        self.assertEqual(result.output_payload["route_resolution_strategy"], "first_principles_broad_variety_overview")

    def test_generic_variety_name_routes_to_overview_instead_of_single_library(self) -> None:
        capability = SQLQueryIntentRouteCapability()
        request = make_request("sql_query.intent_route", input_payload={"user_question": "查询品种龙粳33"})

        result = asyncio.run(capability.execute(request))

        self.assertIsNone(result.interrupt)
        self.assertEqual(result.output_payload["route_id"], "variety_overview")

    def test_explicit_approval_database_display_name_wins_over_broad_lookup(self) -> None:
        capability = SQLQueryIntentRouteCapability()
        request = make_request(
            "sql_query.intent_route",
            input_payload={"user_question": "那么龙粳18呢？补充信息：route_id=审定品种库吧"},
        )

        result = asyncio.run(capability.execute(request))

        self.assertIsNone(result.interrupt)
        self.assertEqual(result.output_payload["route_id"], "approval_variety_db")
        self.assertEqual(result.output_payload["route_resolution_strategy"], "explicit_route_alias")

    def test_approval_database_common_reversed_name_is_an_explicit_alias(self) -> None:
        capability = SQLQueryIntentRouteCapability()
        request = make_request(
            "sql_query.intent_route",
            input_payload={"user_question": "龙粳18的详细信息\n补充信息：route_id=品种审定库，要他的所有信息。"},
        )

        result = asyncio.run(capability.execute(request))

        self.assertIsNone(result.interrupt)
        self.assertEqual(result.output_payload["route_id"], "approval_variety_db")
        self.assertEqual(result.output_payload["inferred_crop"], "rice")

    def test_explicit_genotype_database_display_name_wins_over_generic_variety_word(self) -> None:
        capability = SQLQueryIntentRouteCapability()
        request = make_request(
            "sql_query.intent_route",
            input_payload={"user_question": "查询品种龙粳33，route_id=基因型数据库"},
        )

        result = asyncio.run(capability.execute(request))

        self.assertIsNone(result.interrupt)
        self.assertEqual(result.output_payload["route_id"], "genotype_db")
        self.assertEqual(result.output_payload["route_resolution_strategy"], "explicit_route_alias")

    def test_approval_route_without_crop_triggers_interrupt(self) -> None:
        capability = SQLQueryIntentRouteCapability()
        request = make_request("sql_query.intent_route", input_payload={"user_question": "查询近五年审定品种有哪些"})

        result = asyncio.run(capability.execute(request))

        self.assertIsNotNone(result.interrupt)
        self.assertEqual(result.interrupt.reason_code, "crop_not_resolved")
