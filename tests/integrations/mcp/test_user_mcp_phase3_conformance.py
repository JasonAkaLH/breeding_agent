from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from src.core.enums import (
    UserMCPHealthStatus,
    UserMCPProtocolPreference,
    UserMCPTransport,
)
from src.core.models import Conversation, Task, UserMCPServer
from src.integrations.mcp.adapter_2026 import (
    MCPInputRequiredOutcome,
    MCPTaskCreatedOutcome,
)
from src.integrations.mcp.endpoint_policy import EndpointPolicy
from src.integrations.mcp.gateway import MCPCallCallbacks, MCPGateway
from src.integrations.mcp.protocol import SUPPORTED_MCP_PROTOCOL_VERSION_ORDER
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


FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "fixtures" / "mcp" / "messages"


class _PublicResolver:
    def resolve(self, hostname: str, port: int):
        del hostname, port
        return ("8.8.8.8",)


_ENDPOINT_POLICY = EndpointPolicy(resolver=_PublicResolver())


def _fixture(version: str, name: str) -> dict[str, Any]:
    return json.loads((FIXTURE_ROOT / version / name).read_text(encoding="utf-8"))


class _FixtureGatewayAdapter:
    def __init__(self, version: str) -> None:
        self.version = version
        self.initialize_count = 0
        self.list_count = 0
        self.call_count = 0
        self.cancel_count = 0
        self.closed = False
        self.block_next_call = False
        self.call_started = asyncio.Event()
        self.server_capabilities = {"tools": {}}
        if version == "2026-07-28":
            discover = _fixture(version, "server_discover_result.json")
            self.server_capabilities = dict(discover["result"]["capabilities"])
        self.negotiated_session = SimpleNamespace(
            negotiated_protocol_version=version
        )

    async def initialize(self) -> None:
        self.initialize_count += 1
        if self.version == "2026-07-28":
            request = _fixture(self.version, "server_discover_request.json")
            assert request["method"] == "server/discover"
            assert request["params"]["_meta"][
                "io.modelcontextprotocol/protocolVersion"
            ] == self.version
            return
        request = _fixture(self.version, "initialize_request.json")
        result = _fixture(self.version, "initialize_result.json")
        assert request["params"]["protocolVersion"] == self.version
        assert result["result"]["protocolVersion"] == self.version

    async def list_tools(self) -> list[dict[str, Any]]:
        self.list_count += 1
        if self.version == "2026-07-28":
            return list(_fixture(self.version, "tools_list_result.json")["result"]["tools"])
        call = _fixture(self.version, "tools_call_request.json")
        return [
            {
                "name": call["params"]["name"],
                "inputSchema": {"type": "object"},
            }
        ]

    async def call_tool(self, tool_name: str, arguments: dict[str, Any], **kwargs):
        self.call_count += 1
        callback = kwargs.get("request_registered_callback")
        if callback is not None:
            callback(f"request-{self.version}-{self.call_count}")
        expected = _fixture(self.version, "tools_call_request.json")
        assert tool_name == expected["params"]["name"]
        if self.block_next_call:
            self.call_started.set()
            await asyncio.Event().wait()
        return {"protocolVersion": self.version, "arguments": dict(arguments)}

    async def cancel_request(self, request_id: str, *, reason: str = "") -> bool:
        del request_id, reason
        self.cancel_count += 1
        return True

    async def close(self) -> None:
        self.closed = True


class UserMCPPhase3ConformanceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        root = Path(self._tmpdir.name)
        self.engine = create_sqlite_engine(root / "phase3-conformance.sqlite3")
        bootstrap_sqlite_database(self.engine)
        self.storage = SQLiteStorage(create_sqlite_session_factory(self.engine))
        await self.storage.save_conversation(Conversation("conv", "alice"))
        self.adapters: dict[str, _FixtureGatewayAdapter] = {}
        for ordinal, version in enumerate(SUPPORTED_MCP_PROTOCOL_VERSION_ORDER):
            task_id = f"task-{ordinal}"
            server_id = f"server-{ordinal}"
            await self.storage.save_task(Task(task_id, "conv", f"message-{ordinal}"))
            await self.storage.create_user_mcp_server(
                UserMCPServer(
                    server_id=server_id,
                    owner_user_id="alice",
                    display_name=version,
                    routing_description="",
                    endpoint_url="https://fixture.example/mcp",
                    transport=(
                        UserMCPTransport.LEGACY_HTTP_SSE
                        if version == "2024-11-05"
                        else UserMCPTransport.STREAMABLE_HTTP
                    ),
                    protocol_preference=UserMCPProtocolPreference(version),
                    health_status=UserMCPHealthStatus.AVAILABLE,
                )
            )

        async def client_factory(server, credentials, endpoint):
            del credentials
            version = str(server.protocol_preference)
            adapter = _FixtureGatewayAdapter(version)
            self.adapters[server.server_id] = adapter
            return adapter

        self.result_store = MCPTemporaryResultStore(
            root / "results", memory_threshold_bytes=1024
        )
        self.capacity = MCPTemporaryResultCapacity(
            MCPTemporaryResultCapacityConfig(5, 1),
            storage_root=root / "results",
            free_bytes=lambda _path: 1_000_000,
        )
        self.gateway = MCPGateway(
            storage=self.storage,
            gateway_instance_id="phase3-conformance",
            credential_loader=lambda _server: {},
            client_factory=client_factory,
            endpoint_revalidator=lambda server: _ENDPOINT_POLICY.validate(
                server.endpoint_url
            ),
            result_store=self.result_store,
            capacity=self.capacity,
        )

    async def asyncTearDown(self) -> None:
        await self.gateway.aclose()
        self.engine.dispose()
        self._tmpdir.cleanup()

    async def test_all_five_fixture_versions_execute_through_user_scoped_gateway(self) -> None:
        for ordinal, version in enumerate(SUPPORTED_MCP_PROTOCOL_VERSION_ORDER):
            with self.subTest(version=version):
                task_id = f"task-{ordinal}"
                server_id = f"server-{ordinal}"
                scope = await self.gateway.open_scope(
                    SimpleNamespace(username="alice"), task_id, server_id
                )
                catalog = await self.gateway.list_tools(scope)
                outcome = await self.gateway.call_tool(
                    scope, catalog.tools[0].name, {"tenant": "alpha"}
                )

                self.assertEqual(catalog.effective_protocol_version, version)
                self.assertEqual(outcome.kind.value, "completed")
                self.assertEqual(self.adapters[server_id].initialize_count, 1)
                self.assertEqual(self.adapters[server_id].list_count, 1)
                self.assertEqual(self.adapters[server_id].call_count, 1)
                await self.gateway.close_scope(scope, "fixture_complete")
                self.assertTrue(self.adapters[server_id].closed)

        self.assertEqual(self.capacity.active_calls, 0)
        self.assertEqual(
            await self.storage.list_live_user_mcp_scope_leases(now=datetime.now()), []
        )

    async def test_cancel_and_cleanup_are_gateway_scoped_for_every_version(self) -> None:
        for ordinal, version in enumerate(SUPPORTED_MCP_PROTOCOL_VERSION_ORDER):
            with self.subTest(version=version):
                task_id = f"task-{ordinal}"
                server_id = f"server-{ordinal}"
                scope = await self.gateway.open_scope(
                    SimpleNamespace(username="alice"), task_id, server_id
                )
                adapter = self.adapters[server_id]
                adapter.block_next_call = True
                catalog = await self.gateway.list_tools(scope)
                call_refs: list[str] = []
                call = asyncio.create_task(
                    self.gateway.call_tool(
                        scope,
                        catalog.tools[0].name,
                        {"tenant": "alpha"},
                        MCPCallCallbacks(on_registered=call_refs.append),
                    )
                )
                await asyncio.wait_for(adapter.call_started.wait(), timeout=1)
                while not call_refs:
                    await asyncio.sleep(0)

                cancelled = await self.gateway.cancel_call(
                    scope, call_refs[0], "fixture_cancel"
                )

                self.assertEqual(cancelled.status.value, "cancelled")
                self.assertTrue(cancelled.remote_stop_confirmed)
                self.assertEqual(adapter.cancel_count, 1)
                with self.assertRaises(asyncio.CancelledError):
                    await call
                await self.gateway.close_scope(scope, "fixture_cleanup")

        self.assertEqual(self.capacity.active_calls, 0)

    async def test_2026_only_mrtr_and_tasks_outcomes_are_not_exposed_by_older_scopes(self) -> None:
        for ordinal, version in enumerate(SUPPORTED_MCP_PROTOCOL_VERSION_ORDER[:-1]):
            with self.subTest(version=version):
                scope = await self.gateway.open_scope(
                    SimpleNamespace(username="alice"),
                    f"task-{ordinal}",
                    f"server-{ordinal}",
                )
                adapter = self.adapters[f"server-{ordinal}"]
                catalog = await self.gateway.list_tools(scope)
                outcome = await self.gateway.call_tool(
                    scope, catalog.tools[0].name, {"tenant": "alpha"}
                )

                self.assertEqual(outcome.kind.value, "completed")
                self.assertNotIn("extensions", adapter.server_capabilities)
                await self.gateway.close_scope(scope, "old_version_complete")

        modern_scope = await self.gateway.open_scope(
            SimpleNamespace(username="alice"), "task-4", "server-4"
        )
        modern = self.adapters["server-4"]
        self.assertIn("io.modelcontextprotocol/tasks", modern.server_capabilities["extensions"])

        async def input_required(*_args, **_kwargs):
            fixture = _fixture("2026-07-28", "input_required_result.json")["result"]
            return MCPInputRequiredOutcome(
                input_requests=fixture["inputRequests"],
                sealed_request_state_ref="sealed:fixture",
            )

        modern.call_tool = input_required
        pending = await self.gateway.call_tool(modern_scope, "lookup", {"tenant": "alpha"})
        self.assertEqual(pending.kind.value, "input_required")

        async def task_created(*_args, **_kwargs):
            fixture = _fixture("2026-07-28", "create_task_result.json")["result"]
            return MCPTaskCreatedOutcome(
                safe_remote_task_ref="remote:fixture",
                status=fixture["status"],
                ttl_ms=fixture["ttlMs"],
                poll_interval_ms=fixture["pollIntervalMs"],
            )

        modern.call_tool = task_created
        task = await self.gateway.call_tool(modern_scope, "lookup", {"tenant": "alpha"})
        self.assertEqual(task.kind.value, "task_created")
        await self.gateway.close_scope(modern_scope, "modern_complete")


if __name__ == "__main__":
    unittest.main()
