from __future__ import annotations

from tests.api.support import APITestCase

SESSION_COOKIE_NAME = "__Host-maf_session"
LEGACY_SESSION_COOKIE_NAME = "maf_session"


class AuthCookieSecurityAPITest(APITestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.logout()

    async def test_login_does_not_set_session_cookie(self) -> None:
        response = await self.client.post("/api/v1/auth/login", json={"username": "alice"})

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(SESSION_COOKIE_NAME, self.client.cookies)
        self.assertNotIn(LEGACY_SESSION_COOKIE_NAME, self.client.cookies)
        self.assertEqual(response.headers.get_list("set-cookie"), [])

    async def test_me_rejects_new_and_legacy_session_cookies(self) -> None:
        self.client.cookies.set(SESSION_COOKIE_NAME, "unused-session", path="/")
        self.client.cookies.set(LEGACY_SESSION_COOKIE_NAME, "legacy-session", path="/")

        response = await self.client.get("/api/v1/auth/me")

        self.assertEqual(response.status_code, 401)

    async def test_logout_clears_current_token_without_cookie_cleanup_fallback(self) -> None:
        await self.login("alice")
        self.client.cookies.set(SESSION_COOKIE_NAME, "unused-session", path="/")
        self.client.cookies.set(LEGACY_SESSION_COOKIE_NAME, "legacy-session", path="/")

        response = await self.client.post("/api/v1/auth/logout")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get_list("set-cookie"), [])
        self.client.headers.pop("Authorization", None)
        self.assertEqual((await self.client.get("/api/v1/auth/me")).status_code, 401)
