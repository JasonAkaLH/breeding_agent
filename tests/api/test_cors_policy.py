from __future__ import annotations

import os

from src.api.app import create_app
from tests.api.support import APITestCase


class CorsPolicyAPITest(APITestCase):
    async def asyncSetUp(self) -> None:
        self._previous = os.environ.get("MAF_API_CORS_ALLOWED_ORIGINS")
        await super().asyncSetUp()

    async def asyncTearDown(self) -> None:
        if self._previous is None:
            os.environ.pop("MAF_API_CORS_ALLOWED_ORIGINS", None)
        else:
            os.environ["MAF_API_CORS_ALLOWED_ORIGINS"] = self._previous
        await super().asyncTearDown()

    async def test_no_cors_origin_is_allowed_by_default(self) -> None:
        response = await self.client.options(
            "/api/v1/conversations",
            headers={
                "Origin": "https://third.example",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "Authorization",
            },
        )
        self.assertNotEqual(response.headers.get("access-control-allow-origin"), "https://third.example")

    async def test_allowlisted_origin_can_preflight_authorization_header(self) -> None:
        os.environ["MAF_API_CORS_ALLOWED_ORIGINS"] = " https://third.example,https://third.example "
        await self.reconfigure_runtime()
        response = await self.client.options(
            "/api/v1/conversations",
            headers={
                "Origin": "https://third.example",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "Authorization",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("access-control-allow-origin"), "https://third.example")
        self.assertIn("Authorization", response.headers.get("access-control-allow-headers", ""))
        self.assertNotEqual(response.headers.get("access-control-allow-credentials"), "true")

    async def test_wildcard_origin_fails_closed(self) -> None:
        os.environ["MAF_API_CORS_ALLOWED_ORIGINS"] = "*"
        with self.assertRaises(ValueError):
            create_app(runtime=self.runtime)
