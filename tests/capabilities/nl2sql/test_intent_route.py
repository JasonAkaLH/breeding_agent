from __future__ import annotations

import asyncio
import unittest

from src.capabilities.nl2sql.intent_route import NL2SQLIntentRouteCapability

from tests.capabilities.nl2sql.support import make_request


class NL2SQLIntentRouteTest(unittest.TestCase):
    def test_genotype_question_routes_to_genotype_db(self) -> None:
        capability = NL2SQLIntentRouteCapability()
        request = make_request("nl2sql.intent_route", input_payload={"user_question": "查询某个品种的基因型信息"})

        result = asyncio.run(capability.execute(request))

        self.assertIsNone(result.interrupt)
        self.assertEqual(result.output_payload["route_id"], "genotype_db")
        self.assertEqual(result.output_payload["schema_profile_id"], "genotype_profile")

    def test_approval_route_without_crop_triggers_interrupt(self) -> None:
        capability = NL2SQLIntentRouteCapability()
        request = make_request("nl2sql.intent_route", input_payload={"user_question": "查询近五年审定品种有哪些"})

        result = asyncio.run(capability.execute(request))

        self.assertIsNotNone(result.interrupt)
        self.assertEqual(result.interrupt.reason_code, "crop_not_resolved")
