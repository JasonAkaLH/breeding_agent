from __future__ import annotations

from tests.api.support import APITestCase


class AuthorizationUsernameAuthContractTest(APITestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.logout()
        self.client.cookies.clear()

    async def _login_username(self, username: str) -> str:
        response = await self.client.post("/api/v1/auth/login", json={"username": username})
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["user"], {"username": username})
        token = payload.get("access_token")
        self.assertIsInstance(token, str)
        self.assertTrue(token.startswith("maf_tok_"), token)
        self.assertNotIn("set-cookie", {key.lower(): value for key, value in response.headers.items()})
        self.assertNotIn("__Host-maf_session", self.client.cookies)
        self.assertNotIn("maf_session", self.client.cookies)
        return token

    async def test_login_is_username_only_and_me_requires_bearer(self) -> None:
        token = await self._login_username("alice")

        no_bearer = await self.client.get("/api/v1/auth/me")
        self.assertEqual(no_bearer.status_code, 401)

        malformed = await self.client.get("/api/v1/auth/me", headers={"Authorization": "Basic abc"})
        self.assertEqual(malformed.status_code, 401)

        me = await self.client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(me.status_code, 200, me.text)
        self.assertEqual(me.json(), {"user": {"username": "alice"}})

    async def test_login_refresh_and_logout_keep_only_one_current_token(self) -> None:
        first = await self._login_username("alice")
        second = await self._login_username("alice")
        self.assertNotEqual(first, second)

        self.assertEqual((await self.client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {first}"})).status_code, 401)
        self.assertEqual((await self.client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {second}"})).status_code, 200)

        refreshed = await self.client.post("/api/v1/auth/refresh-token", headers={"Authorization": f"Bearer {second}"})
        self.assertEqual(refreshed.status_code, 200, refreshed.text)
        third = refreshed.json()["access_token"]
        self.assertNotEqual(second, third)
        self.assertEqual((await self.client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {second}"})).status_code, 401)
        self.assertEqual((await self.client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {third}"})).status_code, 200)

        logout = await self.client.post("/api/v1/auth/logout", headers={"Authorization": f"Bearer {third}"})
        self.assertEqual(logout.status_code, 200, logout.text)
        self.assertEqual(logout.json(), {"logged_out": True})
        self.assertEqual((await self.client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {third}"})).status_code, 401)

    async def test_legacy_cookie_captcha_register_and_token_management_routes_are_not_auth_paths(self) -> None:
        self.client.cookies.set("__Host-maf_session", "sess-legacy", domain="testserver")
        self.client.cookies.set("maf_session", "sess-legacy", domain="testserver")
        cookie_only = await self.client.get("/api/v1/auth/me")
        self.assertEqual(cookie_only.status_code, 401)

        for method, path, kwargs in (
            ("POST", "/api/v1/auth/captcha", {}),
            ("POST", "/api/v1/auth/register", {"json": {"username": "alice"}}),
            ("POST", "/api/v1/auth/api-tokens", {"json": {"client_name": "client", "scopes": []}}),
            ("GET", "/api/v1/auth/api-tokens", {}),
            ("DELETE", "/api/v1/auth/api-tokens", {"json": {"token_id": "tok-1"}}),
        ):
            response = await self.client.request(method, path, **kwargs)
            self.assertIn(response.status_code, {404, 410}, f"{method} {path}: {response.text}")

    async def test_submit_rejects_body_owner_fields_and_uses_bearer_username(self) -> None:
        token = await self._login_username("alice")
        self.client.headers["Authorization"] = f"Bearer {token}"

        top_level_spoof = await self.client.post(
            "/api/v1/conversations/chat-messages",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "conversation_id": "conv-alice",
                "username": "mallory",
                "content": "你好",
                "routing_mode": "auto",
                "capability_id": None,
                "metadata": {},
            },
        )
        self.assertEqual(top_level_spoof.status_code, 422, top_level_spoof.text)

        metadata_spoof = await self.client.post(
            "/api/v1/conversations/chat-messages",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "conversation_id": "conv-alice",
                "content": "你好",
                "routing_mode": "auto",
                "capability_id": None,
                "metadata": {"account_id": "mallory", "username": "mallory"},
            },
        )
        self.assertEqual(metadata_spoof.status_code, 422, metadata_spoof.text)

        nested_metadata_spoof = await self.client.post(
            "/api/v1/conversations/chat-messages",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "conversation_id": "conv-alice",
                "content": "你好",
                "routing_mode": "auto",
                "capability_id": None,
                "metadata": {"nested": {"ACCOUNT_ID": "mallory"}, "items": [{"Username": "mallory"}]},
            },
        )
        self.assertEqual(nested_metadata_spoof.status_code, 422, nested_metadata_spoof.text)

        for forbidden_key in ("session", "auth_token", "bearerToken", "captcha", "password_hash", "identity", "owner"):
            with self.subTest(forbidden_key=forbidden_key):
                response = await self.client.post(
                    "/api/v1/conversations/chat-messages",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "conversation_id": "conv-alice",
                        "content": "你好",
                        "routing_mode": "auto",
                        "capability_id": None,
                        "metadata": {"nested": [{forbidden_key: "spoof"}]},
                    },
                )
                self.assertEqual(response.status_code, 422, response.text)

        response = await self.client.post(
            "/api/v1/conversations/chat-messages",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "conversation_id": "conv-alice",
                "content": "你好",
                "routing_mode": "auto",
                "capability_id": None,
                "metadata": {},
            },
        )
        self.assertEqual(response.status_code, 202, response.text)
        await self.wait_for_terminal_task(response.json()["task_id"])

        conversation = await self.runtime.storage.get_conversation("conv-alice")
        self.assertIsNotNone(conversation)
        self.assertEqual(conversation.username, "alice")
        self.assertFalse(hasattr(conversation, "account_id"))

        listed = await self.client.get("/api/v1/conversations", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(listed.status_code, 200, listed.text)
        item = listed.json()["conversations"][0]
        self.assertEqual(item["username"], "alice")
        self.assertNotIn("account_id", item)

    async def test_interrupt_answer_rejects_nested_identity_token_and_session_fields(self) -> None:
        token = await self._login_username("alice")

        for forbidden_payload in (
            {"session": "spoof"},
            {"nested": {"authToken": "spoof"}},
            {"items": [{"captcha": "spoof"}]},
            {"identity": {"owner": "mallory"}},
            {"password_hash": "spoof"},
        ):
            with self.subTest(forbidden_payload=forbidden_payload):
                response = await self.client.post(
                    "/api/v1/tasks/interrupts/answer",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "task_id": "task-any",
                        "interrupt_id": "interrupt-any",
                        "answer_payload": forbidden_payload,
                    },
                )
                self.assertEqual(response.status_code, 422, response.text)
