from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from src.api.dto import CreateUserMCPServerRequest, PatchUserMCPServerRequest
from src.integrations.mcp.credentials import CredentialCipher
from src.integrations.mcp.endpoint_policy import EndpointPolicy
from src.integrations.mcp.invalidation import InMemoryMCPInvalidationBus
from src.integrations.mcp.user_config import UserMCPConfigError, UserMCPConfigService
from src.storage.sqlite import (
    SQLiteStorage,
    bootstrap_sqlite_database,
    create_sqlite_engine,
    create_sqlite_session_factory,
)


class _Resolver:
    def resolve(self, hostname: str, port: int):
        del hostname, port
        return ("8.8.8.8",)


class _HealthRunner:
    def __init__(self) -> None:
        self.servers = []

    async def start_test(self, server):
        self.servers.append(server)
        await self.storage.update_user_mcp_server(
            server.owner_user_id,
            server.server_id,
            changes={"health_status": "testing"},
            updated_at=datetime(2026, 8, 12, 10, 0, 0),
        )
        return f"attempt-{len(self.servers)}"


class UserMCPConfigServiceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.engine = create_sqlite_engine(Path(self.temp_dir.name) / "test.sqlite3")
        self.session_factory = create_sqlite_session_factory(self.engine)
        bootstrap_sqlite_database(self.engine)
        self.storage = SQLiteStorage(self.session_factory)
        self.health = _HealthRunner()
        self.health.storage = self.storage
        self.bus = InMemoryMCPInvalidationBus()
        self.service = UserMCPConfigService(
            storage=self.storage,
            credential_cipher=CredentialCipher(b"k" * 32),
            endpoint_policy=EndpointPolicy(resolver=_Resolver()),
            health_runner=self.health,
            invalidation_bus=self.bus,
            now_fn=lambda: datetime(2026, 8, 12, 10, 0, 0),
        )

    async def asyncTearDown(self) -> None:
        self.engine.dispose()
        self.temp_dir.cleanup()

    async def test_create_encrypts_credentials_and_isolates_owner(self) -> None:
        request = CreateUserMCPServerRequest(
            display_name="Shared",
            endpoint_url="https://public.example/mcp",
            auth_type="bearer",
            credential={"secret_value": "secret-token"},
        )
        server = await self.service.create_server("alice", request.model_dump())

        self.assertEqual(server.owner_user_id, "alice")
        self.assertTrue(server.credential_configured)
        self.assertEqual(server.health_status.value, "testing")
        self.assertEqual(len(self.health.servers), 1)
        record = await self.storage.get_user_mcp_credential("alice", server.server_id)
        self.assertIsNotNone(record)
        self.assertNotIn(b"secret-token", record.credential_ciphertext)
        with self.assertRaisesRegex(UserMCPConfigError, "mcp_server_not_found"):
            await self.service.get_server("bob", server.server_id)

    async def test_security_patch_replaces_credential_and_publishes_invalidation(self) -> None:
        received = []
        self.bus.subscribe(received.append)
        server = await self.service.create_server(
            "alice",
            CreateUserMCPServerRequest(
                display_name="Server",
                endpoint_url="https://public.example/mcp",
                auth_type="bearer",
                credential={"secret_value": "old"},
            ).model_dump(),
        )
        updated = await self.service.patch_server(
            "alice",
            server.server_id,
            PatchUserMCPServerRequest(
                auth_type="api_key_header",
                auth_metadata={"header_name": "X-API-Key"},
                credential_action="replace",
                credential={"secret_value": "new"},
            ).model_dump(exclude_unset=True),
        )

        self.assertEqual(updated.auth_type.value, "api_key_header")
        self.assertEqual(updated.auth_metadata, {"header_name": "x-api-key"})
        self.assertEqual(updated.security_version, 2)
        self.assertEqual(received[-1].action.value, "security_updated")

    async def test_zero_tool_status_is_not_managed_by_configuration_create(self) -> None:
        server = await self.service.create_server(
            "alice",
            CreateUserMCPServerRequest(
                display_name="No auth",
                endpoint_url="https://public.example/mcp",
            ).model_dump(),
        )
        self.assertEqual(server.health_status.value, "testing")
        self.assertFalse(server.credential_configured)

    async def test_metadata_only_patch_does_not_increment_security_version(self) -> None:
        server = await self.service.create_server(
            "alice",
            CreateUserMCPServerRequest(
                display_name="Before",
                endpoint_url="https://public.example/mcp",
            ).model_dump(),
        )

        updated = await self.service.patch_server(
            "alice",
            server.server_id,
            PatchUserMCPServerRequest(display_name="After").model_dump(
                exclude_unset=True
            ),
        )

        self.assertEqual(updated.security_version, server.security_version)
        self.assertGreater(updated.config_version, server.config_version)

    async def test_deletion_coordinator_survives_transient_storage_error(self) -> None:
        calls = 0

        async def reconcile() -> int:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("temporary database error")
            self.service._closing = True
            return 0

        async def no_sleep(_seconds: float) -> None:
            return None

        self.service.reconcile_deletions_once = reconcile
        self.service._closing = False
        with patch("src.integrations.mcp.user_config.asyncio.sleep", no_sleep):
            await self.service._deletion_loop()

        self.assertEqual(calls, 2)
