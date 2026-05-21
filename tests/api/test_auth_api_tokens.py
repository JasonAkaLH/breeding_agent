from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
from unittest.mock import patch

from src.auth import ApiTokenService, AuthTokenValidationError
from src.api.runtime import build_api_runtime
from tests.api.support import APITestCase


class ApiTokenServiceConfigTest(unittest.TestCase):
    def test_token_hash_secret_can_be_required_fail_closed(self) -> None:
        with self.assertRaises(AuthTokenValidationError) as ctx:
            ApiTokenService(object(), now_fn=lambda: datetime(2026, 5, 21), secret=None, require_secret=True)
        self.assertEqual(ctx.exception.code, "token_secret_required")

    def test_production_runtime_requires_token_hash_secret(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict("os.environ", {"MAF_API_ENV": "production"}, clear=True):
                with self.assertRaises(AuthTokenValidationError) as ctx:
                    build_api_runtime(
                        database_path=Path(tmpdir) / "api.sqlite3",
                        audit_log_path=Path(tmpdir) / "audit.jsonl",
                    )
        self.assertEqual(ctx.exception.code, "token_secret_required")


class AuthApiTokenAPITest(APITestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.logout()
        await self.runtime.create_user("alice", "alice-password1")
        await self.runtime.create_user("bob", "bob-password1")

    async def _create_token(self, username: str, scopes: list[str], *, client_name: str = "client") -> tuple[str, str]:
        await self.login(username, f"{username}-password1")
        response = await self.client.post(
            "/api/v1/auth/api-tokens",
            json={"client_name": client_name, "scopes": scopes, "ttl_seconds": 3600},
        )
        self.assertEqual(response.status_code, 201, response.text)
        payload = response.json()
        return payload["token_id"], payload["access_token"]

    async def test_create_list_and_revoke_api_token_never_returns_stored_plaintext(self) -> None:
        token_id, access_token = await self._create_token("alice", ["conversation:read", "conversation:write"])
        stored = await self.runtime.storage.get_auth_api_token(token_id)
        self.assertIsNotNone(stored)
        self.assertNotEqual(stored.token_hash, access_token)
        self.assertNotIn(access_token, repr(stored))

        listed = await self.client.get("/api/v1/auth/api-tokens")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["tokens"][0]["token_id"], token_id)
        self.assertNotIn("access_token", listed.json()["tokens"][0])

        revoked = await self.client.request("DELETE", "/api/v1/auth/api-tokens", json={"token_id": token_id})
        self.assertEqual(revoked.status_code, 200)
        self.assertTrue(revoked.json()["revoked"])
        self.client.cookies.clear()
        me = await self.client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {access_token}"})
        self.assertEqual(me.status_code, 401)

    async def test_bearer_token_can_call_business_api_and_ignores_body_account_spoof(self) -> None:
        _token_id, access_token = await self._create_token("alice", ["conversation:read", "conversation:write"])
        self.client.cookies.clear()
        response = await self.client.post(
            "/api/v1/conversations/chat-messages",
            headers={"Authorization": f"Bearer {access_token}"},
            json={
                "conversation_id": "conv-token",
                "account_id": "mallory",
                "content": "你好",
                "routing_mode": "auto",
                "capability_id": None,
                "metadata": {},
            },
        )
        self.assertEqual(response.status_code, 202, response.text)
        conversation = await self.runtime.storage.get_conversation("conv-token")
        self.assertEqual(conversation.account_id, "alice")

        conversations = await self.client.get(
            "/api/v1/conversations",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        self.assertEqual(conversations.status_code, 200)
        self.assertEqual(conversations.json()["conversations"][0]["conversation_id"], "conv-token")

    async def test_bearer_scope_denied_and_bearer_takes_precedence_over_cookie(self) -> None:
        _read_id, read_token = await self._create_token("alice", ["conversation:read"], client_name="read")
        self.client.cookies.clear()
        denied = await self.client.post(
            "/api/v1/conversations/chat-messages",
            headers={"Authorization": f"Bearer {read_token}"},
            json={"conversation_id": "conv-denied", "account_id": "alice", "content": "x", "routing_mode": "auto", "capability_id": None, "metadata": {}},
        )
        self.assertEqual(denied.status_code, 403)

        _bob_id, bob_token = await self._create_token("bob", ["conversation:read", "conversation:write"], client_name="bob")
        await self.login("alice", "alice-password1")
        conflict = await self.client.post(
            "/api/v1/conversations/chat-messages",
            headers={"Authorization": f"Bearer {bob_token}"},
            json={"conversation_id": "conv-bob", "account_id": "alice", "content": "x", "routing_mode": "auto", "capability_id": None, "metadata": {}},
        )
        self.assertEqual(conflict.status_code, 202, conflict.text)
        conversation = await self.runtime.storage.get_conversation("conv-bob")
        self.assertEqual(conversation.account_id, "bob")

    async def test_bearer_cannot_manage_tokens_and_invalid_create_inputs_fail(self) -> None:
        _token_id, access_token = await self._create_token("alice", ["conversation:read"])
        self.client.cookies.clear()
        bearer_list = await self.client.get("/api/v1/auth/api-tokens", headers={"Authorization": f"Bearer {access_token}"})
        self.assertEqual(bearer_list.status_code, 401)

        await self.login("alice", "alice-password1")
        invalid_scope = await self.client.post(
            "/api/v1/auth/api-tokens",
            json={"client_name": "bad", "scopes": ["admin:*"], "ttl_seconds": 3600},
        )
        self.assertEqual(invalid_scope.status_code, 400)
        invalid_ttl = await self.client.post(
            "/api/v1/auth/api-tokens",
            json={"client_name": "bad", "scopes": ["conversation:read"], "ttl_seconds": 999999999},
        )
        self.assertEqual(invalid_ttl.status_code, 400)

    async def test_bearer_logout_does_not_revoke_token_and_cross_user_revoke_is_hidden(self) -> None:
        token_id, access_token = await self._create_token("alice", ["conversation:read"])
        logout = await self.client.post(
            "/api/v1/auth/logout",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        self.assertEqual(logout.status_code, 200)

        self.client.cookies.clear()
        bearer_me = await self.client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {access_token}"})
        self.assertEqual(bearer_me.status_code, 200, bearer_me.text)
        self.assertEqual(bearer_me.json()["user"]["username"], "alice")

        await self.login("bob", "bob-password1")
        denied_revoke = await self.client.request("DELETE", "/api/v1/auth/api-tokens", json={"token_id": token_id})
        self.assertEqual(denied_revoke.status_code, 404)
        stored = await self.runtime.storage.get_auth_api_token(token_id)
        self.assertIsNotNone(stored)
        self.assertIsNone(stored.revoked_at)

    async def test_bearer_token_updates_last_used_and_rejects_expired_or_inactive_user(self) -> None:
        token_id, access_token = await self._create_token("alice", ["conversation:read"])
        stored_before = await self.runtime.storage.get_auth_api_token(token_id)
        self.assertIsNotNone(stored_before)
        self.assertIsNone(stored_before.last_used_at)

        self.client.cookies.clear()
        first_use = await self.client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {access_token}"})
        self.assertEqual(first_use.status_code, 200, first_use.text)
        stored_after = await self.runtime.storage.get_auth_api_token(token_id)
        self.assertIsNotNone(stored_after)
        self.assertIsNotNone(stored_after.last_used_at)

        await self.runtime.storage.save_auth_api_token(
            replace(stored_after, expires_at=self.runtime._utcnow_naive() - timedelta(seconds=1))
        )
        expired = await self.client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {access_token}"})
        self.assertEqual(expired.status_code, 401)

        inactive_token_id, inactive_access_token = await self._create_token("bob", ["conversation:read"])
        bob = await self.runtime.storage.get_auth_user("bob")
        self.assertIsNotNone(bob)
        await self.runtime.storage.save_auth_user(replace(bob, status="disabled"))
        self.client.cookies.clear()
        inactive = await self.client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {inactive_access_token}"})
        self.assertEqual(inactive.status_code, 401)
        inactive_token = await self.runtime.storage.get_auth_api_token(inactive_token_id)
        self.assertIsNotNone(inactive_token)
        self.assertIsNone(inactive_token.revoked_at)
