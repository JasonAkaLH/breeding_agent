from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime

from src.core.enums import UserMCPHealthStatus, UserMCPTransport
from src.core.models import UserMCPCredentialRecord, UserMCPServer
from src.storage.sqlite import SQLiteStorage
from tests.storage.support import SQLiteStorageTestCase


class UserMCPServerAtomicCreateTest(SQLiteStorageTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.storage = SQLiteStorage(self.session_factory)
        self.now = datetime(2026, 8, 13, 12, 0, 0)

    def _server(self, server_id: str, *, owner: str = "alice") -> UserMCPServer:
        return UserMCPServer(
            server_id=server_id,
            owner_user_id=owner,
            display_name=server_id,
            routing_description=f"route-{server_id}",
            endpoint_url=f"https://{server_id}.example.test/mcp",
            transport=UserMCPTransport.STREAMABLE_HTTP,
            health_status=UserMCPHealthStatus.UNTESTED,
            created_at=self.now,
            updated_at=self.now,
        )

    def _credential(self, server_id: str, *, owner: str = "alice") -> UserMCPCredentialRecord:
        return UserMCPCredentialRecord(
            owner_user_id=owner,
            server_id=server_id,
            credential_ciphertext=f"cipher-{server_id}".encode(),
            credential_nonce=f"nonce-{server_id}".encode(),
            encryption_version=1,
            credential_updated_at=self.now,
        )

    def test_creates_multiple_servers_and_credentials_in_one_batch(self) -> None:
        first = self._server("server-a")
        second = self._server("server-b")
        credential = self._credential("server-a")

        created = asyncio.run(
            self.storage.create_user_mcp_servers_atomic(
                ((first, credential), (second, None))
            )
        )

        self.assertEqual([server.server_id for server in created], ["server-a", "server-b"])
        self.assertTrue(created[0].credential_configured)
        self.assertFalse(created[1].credential_configured)
        self.assertEqual(
            asyncio.run(self.storage.get_user_mcp_credential("alice", "server-a")),
            credential,
        )

    def test_later_conflict_leaves_earlier_candidate_unwritten(self) -> None:
        existing = self._server("server-b")
        asyncio.run(self.storage.create_user_mcp_server(existing))

        with self.assertRaisesRegex(ValueError, "conflicts"):
            asyncio.run(
                self.storage.create_user_mcp_servers_atomic(
                    (
                        (self._server("server-a"), None),
                        (replace(existing, endpoint_url="https://different.example.test/mcp"), None),
                    )
                )
            )

        self.assertIsNone(asyncio.run(self.storage.get_user_mcp_server("alice", "server-a")))
        self.assertEqual(
            asyncio.run(self.storage.get_user_mcp_server("alice", "server-b")), existing
        )

    def test_invalid_later_credential_scope_leaves_batch_unwritten(self) -> None:
        first = self._server("server-a")
        second = self._server("server-b")

        with self.assertRaisesRegex(ValueError, "credential scope"):
            asyncio.run(
                self.storage.create_user_mcp_servers_atomic(
                    ((first, None), (second, self._credential("server-other")))
                )
            )

        self.assertEqual(asyncio.run(self.storage.list_user_mcp_servers("alice")), [])
    def test_identical_replay_is_idempotent_and_credential_conflict_is_rejected(self) -> None:
        server = self._server("server-a")
        credential = self._credential("server-a")
        first = asyncio.run(
            self.storage.create_user_mcp_servers_atomic(((server, credential),))
        )
        replay = asyncio.run(
            self.storage.create_user_mcp_servers_atomic(((server, credential),))
        )

        self.assertEqual(replay, first)
        self.assertEqual(len(asyncio.run(self.storage.list_user_mcp_servers("alice"))), 1)
        with self.assertRaisesRegex(ValueError, "conflicts"):
            asyncio.run(
                self.storage.create_user_mcp_servers_atomic(
                    ((server, replace(credential, credential_ciphertext=b"different")),)
                )
            )
        self.assertEqual(
            asyncio.run(self.storage.get_user_mcp_credential("alice", "server-a")),
            credential,
        )

    def test_duplicate_server_identity_inside_batch_is_rejected_without_writes(self) -> None:
        server = self._server("server-a")
        with self.assertRaisesRegex(ValueError, "duplicate"):
            asyncio.run(
                self.storage.create_user_mcp_servers_atomic(
                    ((server, None), (server, None))
                )
            )
        self.assertEqual(asyncio.run(self.storage.list_user_mcp_servers("alice")), [])
