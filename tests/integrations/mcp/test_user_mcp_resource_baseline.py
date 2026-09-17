from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from src.core.enums import UserMCPHealthStatus, UserMCPTransport
from src.core.models import Conversation, Task, UserMCPServer
from src.integrations.mcp.endpoint_policy import EndpointPolicy
from src.integrations.mcp.gateway import MCPGateway
from src.integrations.mcp.temporary_results import (
    MCPTemporaryResultCapacity,
    MCPTemporaryResultCapacityConfig,
    MCPTemporaryResultStore,
)
from src.storage.sqlite import (
    SQLiteStorage,
    bootstrap_sqlite_database,
    create_sqlite_engine,
    create_sqlite_session_factory,
)


class _ResourceAdapter:
    def __init__(self) -> None:
        self.server_capabilities = {"tools": {}}
        self.negotiated_session = SimpleNamespace(
            negotiated_protocol_version="2025-11-25"
        )
        self.list_count = 0
        self.closed = False

    async def initialize(self) -> None:
        return None

    async def list_tools(self):
        self.list_count += 1
        return [{"name": "echo", "inputSchema": {"type": "object"}}]

    async def close(self) -> None:
        self.closed = True


class _PublicResolver:
    def resolve(self, hostname: str, port: int):
        del hostname, port
        return ("8.8.8.8",)


_ENDPOINT_POLICY = EndpointPolicy(resolver=_PublicResolver())


class UserMCPResourceBaselineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        root = Path(self._tmpdir.name)
        self.engine = create_sqlite_engine(root / "resource-baseline.sqlite3")
        bootstrap_sqlite_database(self.engine)
        self.storage = SQLiteStorage(create_sqlite_session_factory(self.engine))
        self.credentials_loaded = 0
        self.clients_created = 0
        self.adapters: list[_ResourceAdapter] = []

        async def credential_loader(_server):
            self.credentials_loaded += 1
            return {}

        async def client_factory(_server, _credentials, _endpoint):
            self.clients_created += 1
            adapter = _ResourceAdapter()
            self.adapters.append(adapter)
            return adapter

        self.result_root = root / "results"
        self.capacity = MCPTemporaryResultCapacity(
            MCPTemporaryResultCapacityConfig(1, 1),
            storage_root=self.result_root,
            free_bytes=lambda _path: 1_000_000,
        )
        self.gateway = MCPGateway(
            storage=self.storage,
            gateway_instance_id="resource-baseline",
            credential_loader=credential_loader,
            client_factory=client_factory,
            endpoint_revalidator=lambda server: _ENDPOINT_POLICY.validate(
                server.endpoint_url
            ),
            result_store=MCPTemporaryResultStore(
                self.result_root, memory_threshold_bytes=1024
            ),
            capacity=self.capacity,
        )

    async def asyncTearDown(self) -> None:
        await self.gateway.aclose()
        self.engine.dispose()
        self._tmpdir.cleanup()

    async def test_configured_but_unused_users_allocate_no_clients_or_catalogs(self) -> None:
        for ordinal in range(250):
            owner = f"configured-user-{ordinal}"
            await self.storage.create_user_mcp_server(
                UserMCPServer(
                    server_id=f"server-{ordinal}",
                    owner_user_id=owner,
                    display_name="Configured only",
                    routing_description="",
                    endpoint_url="https://fixture.example/mcp",
                    transport=UserMCPTransport.STREAMABLE_HTTP,
                    health_status=UserMCPHealthStatus.AVAILABLE,
                )
            )

        self.assertEqual(self.credentials_loaded, 0)
        self.assertEqual(self.clients_created, 0)
        self.assertEqual(self.adapters, [])
        self.assertEqual(self.capacity.active_calls, 0)
        self.assertEqual(self.capacity.queued_calls, 0)
        self.assertEqual(self.gateway._scopes, {})  # noqa: SLF001 - resource baseline
        self.assertEqual(self.gateway._opening, {})  # noqa: SLF001 - resource baseline

    async def test_queued_scope_does_not_decrypt_connect_or_build_catalog(self) -> None:
        await self.storage.save_conversation(Conversation("conv", "alice"))
        await self.storage.save_task(Task("task", "conv", "message"))
        await self.storage.create_user_mcp_server(
            UserMCPServer(
                server_id="server",
                owner_user_id="alice",
                display_name="Queued",
                routing_description="",
                endpoint_url="https://fixture.example/mcp",
                transport=UserMCPTransport.STREAMABLE_HTTP,
                health_status=UserMCPHealthStatus.AVAILABLE,
            )
        )
        holder = await self.capacity.acquire("holder", "occupied-slot")
        opening = asyncio.create_task(
            self.gateway.open_scope(
                SimpleNamespace(username="alice"), "task", "server"
            )
        )
        while self.capacity.queued_calls != 1:
            await asyncio.sleep(0)

        self.assertEqual(self.credentials_loaded, 0)
        self.assertEqual(self.clients_created, 0)
        self.assertEqual(sum(adapter.list_count for adapter in self.adapters), 0)

        opening.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await opening
        await self.gateway.close_task("task", "queued_task_cancelled")
        await holder.release()
        self.assertEqual(self.capacity.active_calls, 0)
        self.assertEqual(self.capacity.queued_calls, 0)


if __name__ == "__main__":
    unittest.main()
