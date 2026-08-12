from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from src.core.enums import UserMCPAuthType, UserMCPHealthStatus, UserMCPTransport
from src.core.models import (
    MCPCredentialKeyValidation,
    UserMCPCredentialRecord,
    UserMCPHealthAttempt,
    UserMCPScopeLease,
    UserMCPServer,
)
from src.storage.sqlite import SQLiteStorage
from tests.storage.support import SQLiteStorageTestCase


class UserMCPServerRepositoryTest(SQLiteStorageTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.storage = SQLiteStorage(self.session_factory)
        self.now = datetime(2026, 8, 12, 12, 0, 0)

    def _server(self, owner: str, server_id: str, *, health: UserMCPHealthStatus = UserMCPHealthStatus.UNTESTED) -> UserMCPServer:
        return UserMCPServer(
            server_id=server_id,
            owner_user_id=owner,
            display_name="shared name",
            routing_description="route",
            endpoint_url="https://example.test/mcp",
            transport=UserMCPTransport.STREAMABLE_HTTP,
            health_status=health,
            created_at=self.now,
            updated_at=self.now,
        )

    def test_owner_scoped_crud_and_credential_operations(self) -> None:
        alice = self._server("alice", "server-a")
        bob = self._server("bob", "server-b")
        credential = UserMCPCredentialRecord(
            owner_user_id="alice",
            server_id="server-a",
            credential_ciphertext=b"cipher",
            credential_nonce=b"nonce",
            encryption_version=1,
            credential_updated_at=self.now,
        )
        asyncio.run(self.storage.create_user_mcp_server(alice, credential))
        asyncio.run(self.storage.create_user_mcp_server(bob))

        self.assertEqual([item.server_id for item in asyncio.run(self.storage.list_user_mcp_servers("alice"))], ["server-a"])
        self.assertIsNone(asyncio.run(self.storage.get_user_mcp_server("bob", "server-a")))
        self.assertEqual(asyncio.run(self.storage.get_user_mcp_credential("alice", "server-a")), credential)

        renamed = asyncio.run(
            self.storage.update_user_mcp_server(
                "alice", "server-a", changes={"display_name": "renamed"}, updated_at=self.now + timedelta(seconds=1)
            )
        )
        self.assertEqual((renamed.config_version, renamed.security_version), (2, 1))
        replaced = UserMCPCredentialRecord("alice", "server-a", b"new", b"new-nonce", 1, self.now)
        secured = asyncio.run(
            self.storage.update_user_mcp_server(
                "alice", "server-a", changes={"auth_type": UserMCPAuthType.BEARER},
                credential_operation="replace", credential=replaced, security_sensitive=True,
                updated_at=self.now + timedelta(seconds=2),
            )
        )
        self.assertEqual((secured.config_version, secured.security_version), (3, 2))
        cleared = asyncio.run(
            self.storage.update_user_mcp_server(
                "alice", "server-a", changes={}, credential_operation="clear",
                updated_at=self.now + timedelta(seconds=3),
            )
        )
        self.assertFalse(cleared.credential_configured)
        self.assertEqual((cleared.config_version, cleared.security_version), (4, 3))

    def test_update_can_require_exact_config_and_security_versions(self) -> None:
        asyncio.run(
            self.storage.create_user_mcp_server(self._server("alice", "server-a"))
        )
        stale = asyncio.run(
            self.storage.update_user_mcp_server(
                "alice",
                "server-a",
                changes={"health_status": UserMCPHealthStatus.UNAVAILABLE},
                expected_config_version=2,
                expected_security_version=1,
                updated_at=self.now + timedelta(seconds=1),
            )
        )
        self.assertIsNone(stale)
        current = asyncio.run(
            self.storage.get_user_mcp_server("alice", "server-a")
        )
        self.assertEqual(current.health_status, UserMCPHealthStatus.UNTESTED)
        self.assertEqual((current.config_version, current.security_version), (1, 1))

    def test_health_attempt_uses_owner_runner_version_and_lease_cas(self) -> None:
        asyncio.run(self.storage.create_user_mcp_server(self._server("alice", "server-a")))
        attempt = UserMCPHealthAttempt(
            "attempt-1", "alice", "server-a", 1, 1, "runner-a",
            self.now + timedelta(seconds=30), self.now, self.now,
        )
        self.assertTrue(asyncio.run(self.storage.claim_user_mcp_health_attempt(attempt)))
        self.assertFalse(asyncio.run(self.storage.claim_user_mcp_health_attempt(attempt)))
        asyncio.run(
            self.storage.update_user_mcp_server(
                "alice", "server-a", changes={"endpoint_url": "https://new.example/mcp"},
                security_sensitive=True, updated_at=self.now + timedelta(seconds=1),
            )
        )
        self.assertFalse(
            asyncio.run(
                self.storage.complete_user_mcp_health_attempt(
                    "attempt-1", "alice", "server-a", runner_instance_id="runner-a",
                    config_version=1, security_version=1, health_status="available", error_code=None,
                    completed_at=self.now + timedelta(seconds=2),
                )
            )
        )
        self.assertEqual(asyncio.run(self.storage.expire_user_mcp_health_attempts(now=self.now + timedelta(seconds=31))), 1)

    def test_concurrent_health_claim_has_one_winner(self) -> None:
        asyncio.run(self.storage.create_user_mcp_server(self._server("alice", "server-a")))
        attempts = [
            UserMCPHealthAttempt(
                f"attempt-{index}", "alice", "server-a", 1, 1, f"runner-{index}",
                self.now + timedelta(seconds=30), self.now, self.now,
            )
            for index in range(2)
        ]

        async def claim_both() -> list[bool]:
            return list(await asyncio.gather(*(self.storage.claim_user_mcp_health_attempt(item) for item in attempts)))

        self.assertEqual(sum(asyncio.run(claim_both())), 1)

    def test_new_version_health_claim_supersedes_live_old_version_and_release_is_exact(self) -> None:
        asyncio.run(self.storage.create_user_mcp_server(self._server("alice", "server-a")))
        old = UserMCPHealthAttempt(
            "attempt-old", "alice", "server-a", 1, 1, "runner-old",
            self.now + timedelta(minutes=5), self.now, self.now,
        )
        self.assertTrue(asyncio.run(self.storage.claim_user_mcp_health_attempt(old)))
        updated = asyncio.run(
            self.storage.update_user_mcp_server(
                "alice", "server-a", changes={"endpoint_url": "https://new.example/mcp"},
                updated_at=self.now + timedelta(seconds=1),
            )
        )
        current = UserMCPHealthAttempt(
            "attempt-current", "alice", "server-a", updated.config_version, updated.security_version,
            "runner-current", self.now + timedelta(minutes=5),
            self.now + timedelta(seconds=1), self.now + timedelta(seconds=1),
        )
        self.assertTrue(asyncio.run(self.storage.claim_user_mcp_health_attempt(current)))
        same_version = UserMCPHealthAttempt(
            "attempt-other", "alice", "server-a", updated.config_version, updated.security_version,
            "runner-other", self.now + timedelta(minutes=5),
            self.now + timedelta(seconds=2), self.now + timedelta(seconds=2),
        )
        self.assertFalse(asyncio.run(self.storage.claim_user_mcp_health_attempt(same_version)))
        self.assertFalse(
            asyncio.run(
                self.storage.release_user_mcp_health_attempt(
                    "attempt-current", "alice", "server-a", runner_instance_id="runner-other",
                    config_version=updated.config_version, security_version=updated.security_version,
                )
            )
        )
        self.assertTrue(
            asyncio.run(
                self.storage.release_user_mcp_health_attempt(
                    "attempt-current", "alice", "server-a", runner_instance_id="runner-current",
                    config_version=updated.config_version, security_version=updated.security_version,
                )
            )
        )
        self.assertFalse(
            asyncio.run(
                self.storage.release_user_mcp_health_attempt(
                    "attempt-current", "alice", "server-a", runner_instance_id="runner-current",
                    config_version=updated.config_version, security_version=updated.security_version,
                )
            )
        )

    def test_scope_lease_fences_disable_and_delete_waits_for_live_leases(self) -> None:
        asyncio.run(
            self.storage.create_user_mcp_server(
                self._server("alice", "server-a", health=UserMCPHealthStatus.AVAILABLE)
            )
        )
        lease = UserMCPScopeLease(
            "scope-1", "alice", "server-a", 1, "gateway-a",
            self.now + timedelta(seconds=30), self.now, self.now,
        )
        self.assertTrue(asyncio.run(self.storage.acquire_user_mcp_scope_lease(lease)))
        tombstone = asyncio.run(
            self.storage.mark_user_mcp_server_deleted("alice", "server-a", deleted_at=self.now + timedelta(seconds=1))
        )
        self.assertTrue(tombstone.deletion_pending)
        self.assertIsNone(asyncio.run(self.storage.get_user_mcp_server("alice", "server-a")))
        self.assertFalse(asyncio.run(self.storage.finalize_user_mcp_server_delete("alice", "server-a", now=self.now + timedelta(seconds=2))))
        self.assertFalse(
            asyncio.run(
                self.storage.renew_user_mcp_scope_lease(
                    "scope-1", "alice", "server-a", gateway_instance_id="gateway-a", security_version=1,
                    lease_expires_at=self.now + timedelta(seconds=60), updated_at=self.now + timedelta(seconds=2),
                )
            )
        )
        self.assertTrue(asyncio.run(self.storage.release_user_mcp_scope_lease("scope-1", gateway_instance_id="gateway-a")))
        self.assertTrue(asyncio.run(self.storage.finalize_user_mcp_server_delete("alice", "server-a", now=self.now + timedelta(seconds=2))))

    def test_pending_tombstones_are_listed_across_owners_for_restart_coordinator(self) -> None:
        asyncio.run(self.storage.create_user_mcp_server(self._server("alice", "server-a")))
        asyncio.run(self.storage.create_user_mcp_server(self._server("bob", "server-b")))
        asyncio.run(
            self.storage.mark_user_mcp_server_deleted("alice", "server-a", deleted_at=self.now)
        )
        asyncio.run(
            self.storage.mark_user_mcp_server_deleted(
                "bob", "server-b", deleted_at=self.now + timedelta(seconds=1)
            )
        )

        pending = asyncio.run(self.storage.list_pending_user_mcp_server_deletions())

        self.assertEqual(
            [(server.owner_user_id, server.server_id) for server in pending],
            [("alice", "server-a"), ("bob", "server-b")],
        )

    def test_key_validation_create_or_get_never_overwrites(self) -> None:
        first = MCPCredentialKeyValidation("singleton", b"nonce-a", b"cipher-a", 1, self.now)
        second = MCPCredentialKeyValidation("other", b"nonce-b", b"cipher-b", 1, self.now)
        self.assertEqual(asyncio.run(self.storage.create_or_get_mcp_credential_key_validation(first)), first)
        self.assertEqual(asyncio.run(self.storage.create_or_get_mcp_credential_key_validation(second)), first)
