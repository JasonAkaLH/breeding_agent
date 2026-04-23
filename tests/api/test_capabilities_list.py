from __future__ import annotations

from tests.api.support import APITestCase


class CapabilitiesListAPITest(APITestCase):
    async def test_capabilities_endpoint_lists_registered_capabilities(self) -> None:
        response = await self.client.get("/api/v1/capabilities")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        capability_ids = {item["capability_id"] for item in payload["capabilities"]}
        self.assertEqual(
            capability_ids,
            {
                "nl2sql.intent_route",
                "nl2sql.schema_context_prepare",
                "nl2sql.sql_generate",
                "nl2sql.sql_guard",
                "nl2sql.sql_execute_readonly",
                "nl2sql.result_summarize",
            },
        )
        self.assertTrue(all(item["status"] == "active" for item in payload["capabilities"]))
