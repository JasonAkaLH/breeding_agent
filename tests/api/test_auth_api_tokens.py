from __future__ import annotations

import pickle
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from src.auth import AuthTokenHasher, UsernameTokenService
from src.api.runtime import build_api_runtime
from src.integrations.master_key import (
    MasterKeyDeriver,
    MasterKeyDomain,
    MasterKeyError,
)
from src.integrations.mcp.credentials import MCPAuditReferenceSigner
from tests.api.support import APITestCase


class UsernameTokenServiceConfigTest(unittest.TestCase):
    def test_auth_token_hasher_rejects_serialization(self) -> None:
        deriver = MasterKeyDeriver.from_bytes(b"a" * 32)
        hasher = AuthTokenHasher(deriver.derive(MasterKeyDomain.AUTH_TOKEN))

        with self.assertRaises(TypeError):
            pickle.dumps(hasher)

    def test_auth_token_hasher_is_stable_and_domain_isolated(self) -> None:
        deriver = MasterKeyDeriver.from_bytes(b"a" * 32)
        hasher = AuthTokenHasher(deriver.derive(MasterKeyDomain.AUTH_TOKEN))
        audit_signer = MCPAuditReferenceSigner(
            deriver.derive(MasterKeyDomain.MCP_AUDIT_REFERENCE)
        )

        token_hash = hasher.hash_token("maf_tok_canary")
        audit_reference = audit_signer.safe_reference(
            "maf_tok_canary",
            context="auth-domain-isolation-v1",
        )

        self.assertEqual(token_hash, hasher.hash_token("maf_tok_canary"))
        self.assertTrue(hasher.verify_token("maf_tok_canary", token_hash))
        self.assertFalse(hasher.verify_token("maf_tok_other", token_hash))
        self.assertNotEqual(token_hash, audit_reference)
        self.assertFalse(
            audit_signer.verify_reference(
                "maf_tok_canary",
                token_hash,
                context="auth-domain-isolation-v1",
            )
        )

    def test_auth_token_hasher_rejects_wrong_domain_key(self) -> None:
        deriver = MasterKeyDeriver.from_bytes(b"a" * 32)

        with self.assertRaises(MasterKeyError) as raised:
            AuthTokenHasher(deriver.derive(MasterKeyDomain.MCP_AUDIT_REFERENCE))

        self.assertEqual(raised.exception.code, "maf_key_domain_invalid")

        with self.assertRaises(MasterKeyError) as raised:
            AuthTokenHasher(b"a" * 32)  # type: ignore[arg-type]

        self.assertEqual(raised.exception.code, "maf_key_domain_invalid")

    def test_username_service_requires_exact_auth_hasher(self) -> None:
        with self.assertRaises(MasterKeyError) as raised:
            UsernameTokenService(
                object(),
                now_fn=lambda: datetime(2026, 5, 25),
                token_hasher=object(),  # type: ignore[arg-type]
            )
        self.assertEqual(raised.exception.code, "maf_key_domain_invalid")

    def test_runtime_uses_master_key_domain_not_text_secret(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(
                "os.environ",
                {
                    "MAF_API_ENV": "test",
                    "MAF_STATE_STORE_BACKEND": "sqlite",
                    "MAF_STATE_PLATFORM_CONFIG_BRIDGE": "0",
                },
                clear=True,
            ):
                runtime = build_api_runtime(
                    database_path=Path(tmpdir) / "api.sqlite3",
                    audit_log_path=Path(tmpdir) / "audit.jsonl",
                    master_key_bytes=b"a" * 32,
                )
                self.assertIsNotNone(runtime.username_token_service)
                runtime._engine.dispose()


class AuthorizationTokenLifecycleAPITest(APITestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.logout()

    async def test_login_persists_only_hash_and_old_routes_are_gone(self) -> None:
        login = await self.client.post("/api/v1/auth/login", json={"username": "alice"})
        self.assertEqual(login.status_code, 200, login.text)
        access_token = login.json()["access_token"]
        stored = await self.runtime.storage.get_auth_user_token("alice")
        self.assertIsNotNone(stored)
        self.assertNotEqual(stored.api_token_hash, access_token)
        self.assertNotIn(access_token, repr(stored))

        for method, path, kwargs in (
            ("POST", "/api/v1/auth/api-tokens", {"json": {"client_name": "client", "scopes": ["conversation:read"]}}),
            ("GET", "/api/v1/auth/api-tokens", {}),
            ("DELETE", "/api/v1/auth/api-tokens", {"json": {"token_id": "tok-old"}}),
        ):
            response = await self.client.request(method, path, headers={"Authorization": f"Bearer {access_token}"}, **kwargs)
            self.assertIn(response.status_code, {404, 410}, f"{method} {path}: {response.text}")

    async def test_authorization_token_can_call_business_api_and_rejects_body_owner_spoof(self) -> None:
        login = await self.client.post("/api/v1/auth/login", json={"username": "alice"})
        self.assertEqual(login.status_code, 200)
        access_token = login.json()["access_token"]

        spoof = await self.client.post(
            "/api/v1/conversations/chat-messages",
            headers={"Authorization": f"Bearer {access_token}"},
            json={
                "conversation_id": "conv-token",
                "username": "mallory",
                "content": "你好",
                "routing_mode": "auto",
                "capability_id": None,
                "metadata": {"account_id": "mallory"},
            },
        )
        self.assertEqual(spoof.status_code, 422, spoof.text)

        response = await self.client.post(
            "/api/v1/conversations/chat-messages",
            headers={"Authorization": f"Bearer {access_token}"},
            json={
                "conversation_id": "conv-token",
                "content": "你好",
                "routing_mode": "auto",
                "capability_id": None,
                "metadata": {},
            },
        )
        self.assertEqual(response.status_code, 202, response.text)
        conversation = await self.runtime.storage.get_conversation("conv-token")
        self.assertEqual(conversation.username, "alice")

        conversations = await self.client.get("/api/v1/conversations", headers={"Authorization": f"Bearer {access_token}"})
        self.assertEqual(conversations.status_code, 200)
        self.assertEqual(conversations.json()["conversations"][0]["conversation_id"], "conv-token")

    async def test_logout_clears_only_current_username_token(self) -> None:
        first = await self.client.post("/api/v1/auth/login", json={"username": "alice"})
        self.assertEqual(first.status_code, 200)
        first_token = first.json()["access_token"]
        second = await self.client.post("/api/v1/auth/login", json={"username": "bob"})
        self.assertEqual(second.status_code, 200)
        second_token = second.json()["access_token"]

        logout = await self.client.post("/api/v1/auth/logout", headers={"Authorization": f"Bearer {first_token}"})
        self.assertEqual(logout.status_code, 200, logout.text)
        alice = await self.runtime.storage.get_auth_user_token("alice")
        self.assertIsNotNone(alice)
        self.assertIsNone(alice.api_token_hash)

        self.assertEqual((await self.client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {first_token}"})).status_code, 401)
        self.assertEqual((await self.client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {second_token}"})).status_code, 200)
