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
                "main_agent.respond",
                "sql_query.query",
            },
        )
        self.assertTrue(all(item["status"] == "active" for item in payload["capabilities"]))
