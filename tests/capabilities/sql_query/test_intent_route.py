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

    def test_approval_route_without_crop_triggers_interrupt(self) -> None:
        capability = SQLQueryIntentRouteCapability()
        request = make_request("sql_query.intent_route", input_payload={"user_question": "查询近五年审定品种有哪些"})

        result = asyncio.run(capability.execute(request))

        self.assertIsNotNone(result.interrupt)
        self.assertEqual(result.interrupt.reason_code, "crop_not_resolved")
