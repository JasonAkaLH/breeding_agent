from __future__ import annotations

from src.api.auth import LEGACY_SESSION_COOKIE_NAME, SESSION_COOKIE_NAME
from tests.api.support import APITestCase


class AuthCookieSecurityAPITest(APITestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.logout()
        await self.runtime.create_user("alice", "alice-password1")

    async def test_login_sets_host_prefixed_cookie_without_user_information(self) -> None:
        response = await self.login("alice", "alice-password1")

        self.assertIn(SESSION_COOKIE_NAME, self.client.cookies)
        self.assertNotIn(LEGACY_SESSION_COOKIE_NAME, self.client.cookies)
        cookie_value = self.client.cookies.get(SESSION_COOKIE_NAME)
        self.assertIsInstance(cookie_value, str)
        self.assertNotIn("alice", cookie_value)
        self.assertNotIn("username", cookie_value.lower())
        set_cookie = response.headers.get("set-cookie", "")
        self.assertIn(f"{SESSION_COOKIE_NAME}=", set_cookie)
        self.assertIn("HttpOnly", set_cookie)
        self.assertIn("Secure", set_cookie)
        self.assertIn("samesite=lax", set_cookie.lower())
        self.assertIn("Path=/", set_cookie)
        self.assertNotIn("Domain=", set_cookie)
        self.assertNotIn("__Host_", set_cookie)

    async def test_me_prefers_new_cookie_but_accepts_legacy_cookie_during_migration(self) -> None:
        alice_login = await self.login("alice", "alice-password1")
        alice_session = self.client.cookies.get(SESSION_COOKIE_NAME)
        await self.runtime.create_user("bob", "bob-password1")
        await self.login("bob", "bob-password1")
        bob_session = self.client.cookies.get(SESSION_COOKIE_NAME)

        self.client.cookies.clear()
        self.client.cookies.set(LEGACY_SESSION_COOKIE_NAME, alice_session, path="/")
        legacy_me = await self.client.get("/api/v1/auth/me")
        self.assertEqual(legacy_me.status_code, 200)
        self.assertEqual(legacy_me.json()["user"]["username"], "alice")

        self.client.cookies.set(SESSION_COOKIE_NAME, bob_session, path="/")
        preferred_me = await self.client.get("/api/v1/auth/me")
        self.assertEqual(preferred_me.status_code, 200)
        self.assertEqual(preferred_me.json()["user"]["username"], "bob")

    async def test_logout_clears_new_and_legacy_cookie_names(self) -> None:
        await self.login("alice", "alice-password1")
        self.client.cookies.set(LEGACY_SESSION_COOKIE_NAME, "legacy-session", path="/")

        response = await self.client.post("/api/v1/auth/logout")

        self.assertEqual(response.status_code, 200)
        set_cookie = response.headers.get_list("set-cookie")
        combined = "\n".join(set_cookie)
        self.assertIn(f"{SESSION_COOKIE_NAME}=", combined)
        self.assertIn(f"{LEGACY_SESSION_COOKIE_NAME}=", combined)
