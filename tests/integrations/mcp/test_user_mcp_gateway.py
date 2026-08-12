from __future__ import annotations

import asyncio
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from src.core.enums import TaskStatus, UserMCPHealthStatus, UserMCPTransport
from src.core.models import Conversation, Task, UserMCPServer
from src.integrations.mcp.client import MCPClientError
from src.integrations.mcp.gateway import MCPCallCallbacks, MCPGateway
from src.integrations.mcp.temporary_results import (
    MCPTemporaryResultCapacity,
    MCPTemporaryResultCapacityConfig,
    MCPTemporaryResultRef,
    MCPTemporaryResultStore,
)
from src.storage.sqlite import (
    SQLiteStorage,
    bootstrap_sqlite_database,
    create_sqlite_engine,
    create_sqlite_session_factory,
)


class _Adapter:
    def __init__(self) -> None:
        self.initialize_count = 0
        self.list_count = 0
        self.call_count = 0
        self.closed = False
        self.server_capabilities = {"tools": {}}
        self.negotiated_session = SimpleNamespace(negotiated_protocol_version="2025-11-25")

    async def initialize(self):
        self.initialize_count += 1

    async def list_tools(self):
        self.list_count += 1
        return [{"name": "echo", "description": "Echo", "inputSchema": {"type": "object"}}]

    async def call_tool(self, tool_name, arguments, **kwargs):
        self.call_count += 1
        callback = kwargs.get("request_registered_callback")
        if callback:
            callback(self.call_count)
        return {"content": [{"type": "text", "text": arguments.get("text", "")}]}

    async def close(self):
        self.closed = True


class _BlockingAdapter(_Adapter):
    def __init__(self, started: asyncio.Event, release: asyncio.Event) -> None:
        super().__init__()
        self._started = started
        self._release = release

    async def initialize(self):
        self.initialize_count += 1
        self._started.set()
        await self._release.wait()


class _TransientAdapter(_Adapter):
    async def initialize(self):
        self.initialize_count += 1
        raise MCPClientError(
            "temporary", code="mcp_transport_error", retriable=True
        )


class _BlockingCallAdapter(_Adapter):
    def __init__(self, started: asyncio.Event) -> None:
        super().__init__()
        self._started = started
        self.cancel_notification_sent = False

    async def call_tool(self, tool_name, arguments, **kwargs):
        del tool_name, arguments
        callback = kwargs.get("request_registered_callback")
        if callback:
            callback("remote-call-1")
        self._started.set()
        await asyncio.Event().wait()

    async def cancel_request(self, request_id, *, reason=""):
        del request_id, reason
        self.cancel_notification_sent = True
        return False


class _SequencedBlockingCallAdapter(_Adapter):
    def __init__(self) -> None:
        super().__init__()
        self.started = (asyncio.Event(), asyncio.Event())
        self.release = (asyncio.Event(), asyncio.Event())

    async def call_tool(self, tool_name, arguments, **kwargs):
        del tool_name, arguments
        ordinal = self.call_count
        self.call_count += 1
        callback = kwargs.get("request_registered_callback")
        if callback:
            callback(f"remote-call-{ordinal + 1}")
        self.started[ordinal].set()
        await self.release[ordinal].wait()
        return {"ordinal": ordinal + 1}


class _ManualHeartbeatWaiter:
    def __init__(self) -> None:
        self.entered: asyncio.Queue[float] = asyncio.Queue()
        self.advance: asyncio.Queue[None] = asyncio.Queue()

    async def __call__(self, signal: asyncio.Event, timeout_seconds: float) -> bool:
        await self.entered.put(timeout_seconds)
        await self.advance.get()
        if not signal.is_set():
            return False
        signal.clear()
        return True


class UserMCPGatewayTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.engine = create_sqlite_engine(root / "gateway.sqlite3")
        bootstrap_sqlite_database(self.engine)
        self.storage = SQLiteStorage(create_sqlite_session_factory(self.engine))
        self.now = datetime(2026, 8, 12, 12, 0, 0)
        await self.storage.save_conversation(Conversation("conv-1", "alice", created_at=self.now, updated_at=self.now))
        await self.storage.save_task(Task("task-1", "conv-1", "msg-1", created_at=self.now, updated_at=self.now))
        await self.storage.create_user_mcp_server(
            UserMCPServer(
                server_id="server-1",
                owner_user_id="alice",
                display_name="Server",
                routing_description="",
                endpoint_url="https://example.com/mcp",
                transport=UserMCPTransport.STREAMABLE_HTTP,
                health_status=UserMCPHealthStatus.AVAILABLE,
                created_at=self.now,
                updated_at=self.now,
            )
        )
        self.adapters = []

        async def credential_loader(server):
            leases = await self.storage.list_live_user_mcp_scope_leases(now=self.now)
            self.assertTrue(leases, "lease must exist before credential decryption")
            return {}

        async def client_factory(server, credentials):
            del server, credentials
            adapter = _Adapter()
            self.adapters.append(adapter)
            return adapter

        self.result_store = MCPTemporaryResultStore(root / "results", memory_threshold_bytes=8)
        self.gateway = MCPGateway(
            storage=self.storage,
            gateway_instance_id="gateway-1",
            credential_loader=credential_loader,
            client_factory=client_factory,
            endpoint_revalidator=lambda server: server.endpoint_url,
            result_store=self.result_store,
            capacity=MCPTemporaryResultCapacity(
                MCPTemporaryResultCapacityConfig(4, 1),
                storage_root=root / "results",
                free_bytes=lambda path: 10_000_000,
            ),
            now_fn=lambda: self.now,
        )

    async def asyncTearDown(self) -> None:
        await self.gateway.aclose()
        self.engine.dispose()
        self.temp_dir.cleanup()

    async def test_concurrent_open_is_single_flight_and_discovery_is_once(self) -> None:
        user = SimpleNamespace(username="alice")
        first, second = await asyncio.gather(
            self.gateway.open_scope(user, "task-1", "server-1"),
            self.gateway.open_scope(user, "task-1", "server-1"),
        )
        self.assertEqual(first, second)
        self.assertEqual(len(self.adapters), 1)
        self.assertEqual(self.adapters[0].list_count, 1)
        self.assertEqual(len((await self.gateway.list_tools(first)).tools), 1)

    async def test_call_returns_opaque_ref_and_close_releases_all(self) -> None:
        scope = await self.gateway.open_scope(
            SimpleNamespace(username="alice"), "task-1", "server-1"
        )
        outcome = await self.gateway.call_tool(scope, "echo", {"text": "hello"})
        self.assertEqual(outcome.kind.value, "completed")
        self.assertTrue(outcome.result_ref.startswith("mcp-result-"))
        await self.gateway.close_task("task-1", "terminal")
        self.assertTrue(self.adapters[0].closed)
        self.assertEqual(
            await self.storage.list_live_user_mcp_scope_leases(now=self.now), []
        )

    async def test_close_scope_preserves_completed_result_until_task_cleanup(self) -> None:
        scope = await self.gateway.open_scope(
            SimpleNamespace(username="alice"), "task-1", "server-1"
        )
        outcome = await self.gateway.call_tool(scope, "echo", {"text": "hello"})
        self.assertEqual(outcome.content_type, "application/json")
        self.assertGreater(outcome.byte_size or 0, 0)
        result_ref = MCPTemporaryResultRef(
            ref=outcome.result_ref or "",
            size_bytes=outcome.byte_size or 0,
            sha256="",
            storage="memory",
        )

        await self.gateway.close_scope(scope, "server_switch")

        payload = b"".join(
            [chunk async for chunk in self.result_store.iter_bytes(result_ref)]
        )
        self.assertIn(b"hello", payload)
        await self.gateway.close_task("task-1", "terminal")
        with self.assertRaises(KeyError):
            _ = [chunk async for chunk in self.result_store.iter_bytes(result_ref)]

    async def test_scope_waits_for_fair_admission_before_credentials_or_client(self) -> None:
        root = Path(self.temp_dir.name) / "queued-results"
        capacity = MCPTemporaryResultCapacity(
            MCPTemporaryResultCapacityConfig(1, 1),
            storage_root=root,
            free_bytes=lambda path: 10_000_000,
        )
        holder = await capacity.acquire("other-user", "holder")
        credentials_loaded = 0
        clients_created = 0

        async def credential_loader(server):
            nonlocal credentials_loaded
            del server
            credentials_loaded += 1
            return {}

        async def client_factory(server, credentials):
            nonlocal clients_created
            del server, credentials
            clients_created += 1
            return _Adapter()

        gateway = MCPGateway(
            storage=self.storage,
            gateway_instance_id="gateway-queued",
            credential_loader=credential_loader,
            client_factory=client_factory,
            endpoint_revalidator=lambda server: server.endpoint_url,
            result_store=MCPTemporaryResultStore(root, memory_threshold_bytes=8),
            capacity=capacity,
            now_fn=lambda: self.now,
        )
        opening = asyncio.create_task(
            gateway.open_scope(
                SimpleNamespace(username="alice"), "task-1", "server-1"
            )
        )
        while capacity.queued_calls != 1:
            await asyncio.sleep(0)
        self.assertEqual(credentials_loaded, 0)
        self.assertEqual(clients_created, 0)

        await holder.release()
        scope = await asyncio.wait_for(opening, timeout=1)
        self.assertEqual(credentials_loaded, 1)
        self.assertEqual(clients_created, 1)
        await gateway.close_scope(scope, "done")
        await gateway.aclose()

    async def test_other_owner_cannot_open_scope(self) -> None:
        with self.assertRaisesRegex(Exception, "mcp_task_not_found"):
            await self.gateway.open_scope(
                SimpleNamespace(username="bob"), "task-1", "server-1"
            )

    async def test_close_task_cancels_scope_that_is_still_opening(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def client_factory(server, credentials):
            del server, credentials
            return _BlockingAdapter(started, release)

        gateway = MCPGateway(
            storage=self.storage,
            gateway_instance_id="gateway-opening",
            credential_loader=lambda server: {},
            client_factory=client_factory,
            endpoint_revalidator=lambda server: server.endpoint_url,
            result_store=self.result_store,
            capacity=MCPTemporaryResultCapacity(
                MCPTemporaryResultCapacityConfig(4, 1),
                storage_root=self.result_store.root,
                free_bytes=lambda path: 10_000_000,
            ),
            now_fn=lambda: self.now,
        )
        opening = asyncio.create_task(
            gateway.open_scope(
                SimpleNamespace(username="alice"), "task-1", "server-1"
            )
        )
        await asyncio.wait_for(started.wait(), timeout=1)

        await gateway.close_task("task-1", "terminal")

        with self.assertRaises(asyncio.CancelledError):
            await opening
        self.assertEqual(
            await self.storage.list_live_user_mcp_scope_leases(now=self.now), []
        )
        release.set()
        await gateway.aclose()

    async def test_close_task_is_a_barrier_against_late_scope_open(self) -> None:
        task = await self.storage.get_task("task-1")
        await self.storage.save_task(replace(task, status=TaskStatus.COMPLETED))
        await self.gateway.close_task("task-1", "terminal")

        with self.assertRaisesRegex(Exception, "mcp_task_not_found"):
            await self.gateway.open_scope(
                SimpleNamespace(username="alice"), "task-1", "server-1"
            )

    async def test_open_scope_renews_lease_while_discovery_is_running(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        renewed = asyncio.Event()
        original_renew = self.storage.renew_user_mcp_scope_lease

        async def recording_renew(*args, **kwargs):
            result = await original_renew(*args, **kwargs)
            renewed.set()
            return result

        self.storage.renew_user_mcp_scope_lease = recording_renew

        async def client_factory(server, credentials):
            del server, credentials
            return _BlockingAdapter(started, release)

        gateway = MCPGateway(
            storage=self.storage,
            gateway_instance_id="gateway-renewing",
            credential_loader=lambda server: {},
            client_factory=client_factory,
            endpoint_revalidator=lambda server: server.endpoint_url,
            result_store=self.result_store,
            capacity=MCPTemporaryResultCapacity(
                MCPTemporaryResultCapacityConfig(4, 1),
                storage_root=self.result_store.root,
                free_bytes=lambda path: 10_000_000,
            ),
            now_fn=lambda: self.now,
            lease_renew_interval_seconds=0.01,
        )
        opening = asyncio.create_task(
            gateway.open_scope(
                SimpleNamespace(username="alice"), "task-1", "server-1"
            )
        )
        try:
            await asyncio.wait_for(started.wait(), timeout=1)
            await asyncio.wait_for(renewed.wait(), timeout=1)
            release.set()
            scope = await asyncio.wait_for(opening, timeout=1)
            self.assertEqual(scope.server_id, "server-1")
        finally:
            release.set()
            if not opening.done():
                opening.cancel()
                await asyncio.gather(opening, return_exceptions=True)
            await gateway.aclose()
            self.storage.renew_user_mcp_scope_lease = original_renew

    async def test_open_scope_retries_transient_discovery_with_fresh_adapter(self) -> None:
        adapters = [_TransientAdapter(), _Adapter()]
        sleeps: list[float] = []

        async def client_factory(server, credentials):
            del server, credentials
            return adapters.pop(0)

        async def record_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        gateway = MCPGateway(
            storage=self.storage,
            gateway_instance_id="gateway-retry",
            credential_loader=lambda server: {},
            client_factory=client_factory,
            endpoint_revalidator=lambda server: server.endpoint_url,
            result_store=self.result_store,
            capacity=MCPTemporaryResultCapacity(
                MCPTemporaryResultCapacityConfig(4, 1),
                storage_root=self.result_store.root,
                free_bytes=lambda path: 10_000_000,
            ),
            now_fn=lambda: self.now,
            discovery_retry_delay_seconds=0.1,
            sleep=record_sleep,
        )
        scope = await gateway.open_scope(
            SimpleNamespace(username="alice"), "task-1", "server-1"
        )

        self.assertEqual(scope.server_id, "server-1")
        self.assertEqual(sleeps, [0.1])
        await gateway.aclose()

    async def test_long_call_emits_heartbeat_without_gateway_timeout(self) -> None:
        started = asyncio.Event()
        heartbeat = asyncio.Event()
        registered: list[str] = []

        async def client_factory(server, credentials):
            del server, credentials
            return _BlockingCallAdapter(started)

        async def on_registered(call_ref: str) -> None:
            registered.append(call_ref)

        async def on_heartbeat(call_ref: str) -> None:
            self.assertEqual(call_ref, registered[0])
            heartbeat.set()

        gateway = MCPGateway(
            storage=self.storage,
            gateway_instance_id="gateway-heartbeat",
            credential_loader=lambda server: {},
            client_factory=client_factory,
            endpoint_revalidator=lambda server: server.endpoint_url,
            result_store=self.result_store,
            capacity=MCPTemporaryResultCapacity(
                MCPTemporaryResultCapacityConfig(4, 1),
                storage_root=self.result_store.root,
                free_bytes=lambda path: 10_000_000,
            ),
            now_fn=lambda: self.now,
            heartbeat_interval_seconds=0.01,
        )
        scope = await gateway.open_scope(
            SimpleNamespace(username="alice"), "task-1", "server-1"
        )
        call = asyncio.create_task(
            gateway.call_tool(
                scope,
                "echo",
                {},
                MCPCallCallbacks(
                    on_registered=on_registered,
                    on_heartbeat=on_heartbeat,
                ),
            )
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        await asyncio.wait_for(heartbeat.wait(), timeout=1)

        self.assertFalse(call.done())
        await gateway.close_scope(scope, "cancel")
        with self.assertRaises(asyncio.CancelledError):
            await call
        await gateway.aclose()

    async def test_continue_resets_heartbeat_cycle_with_injected_waiter(self) -> None:
        started = asyncio.Event()
        waiter = _ManualHeartbeatWaiter()
        refs: list[str] = []
        heartbeats: list[str] = []
        adapter = _BlockingCallAdapter(started)
        gateway = MCPGateway(
            storage=self.storage,
            gateway_instance_id="gateway-heartbeat-reset",
            credential_loader=lambda server: {},
            client_factory=lambda server, credentials: adapter,
            endpoint_revalidator=lambda server: server.endpoint_url,
            result_store=self.result_store,
            capacity=MCPTemporaryResultCapacity(
                MCPTemporaryResultCapacityConfig(4, 1),
                storage_root=self.result_store.root,
                free_bytes=lambda path: 10_000_000,
            ),
            now_fn=lambda: self.now,
            heartbeat_waiter=waiter,
        )
        scope = await gateway.open_scope(
            SimpleNamespace(username="alice"), "task-1", "server-1"
        )
        call = asyncio.create_task(
            gateway.call_tool(
                scope,
                "echo",
                {},
                MCPCallCallbacks(
                    on_created=lambda ref: refs.append(ref),
                    on_heartbeat=lambda ref: heartbeats.append(ref),
                ),
            )
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        self.assertEqual(await asyncio.wait_for(waiter.entered.get(), timeout=1), 120.0)

        continued = await gateway.continue_call(scope, refs[0])
        self.assertEqual(continued.status.value, "reset")
        await waiter.advance.put(None)
        self.assertEqual(await asyncio.wait_for(waiter.entered.get(), timeout=1), 120.0)
        self.assertEqual(heartbeats, [])

        await waiter.advance.put(None)
        while not heartbeats:
            await asyncio.sleep(0)
        self.assertEqual(heartbeats, refs)
        continued_for_task = await gateway.continue_call_for_task("task-1", refs[0])
        self.assertEqual(continued_for_task.status.value, "reset")
        unknown = await gateway.continue_call_for_task("another-task", refs[0])
        self.assertEqual(unknown.status.value, "unknown_call")
        await gateway.close_scope(scope, "cancel")
        with self.assertRaises(asyncio.CancelledError):
            await call
        await gateway.aclose()

    async def test_default_task_guard_serializes_calls_and_queued_call_can_cancel(self) -> None:
        adapter = _SequencedBlockingCallAdapter()
        gateway = MCPGateway(
            storage=self.storage,
            gateway_instance_id="gateway-serial",
            credential_loader=lambda server: {},
            client_factory=lambda server, credentials: adapter,
            endpoint_revalidator=lambda server: server.endpoint_url,
            result_store=self.result_store,
            capacity=MCPTemporaryResultCapacity(
                MCPTemporaryResultCapacityConfig(4, 1),
                storage_root=self.result_store.root,
                free_bytes=lambda path: 10_000_000,
            ),
            now_fn=lambda: self.now,
        )
        scope = await gateway.open_scope(
            SimpleNamespace(username="alice"), "task-1", "server-1"
        )
        first_refs: list[str] = []
        first = asyncio.create_task(
            gateway.call_tool(
                scope,
                "echo",
                {},
                MCPCallCallbacks(on_created=lambda ref: first_refs.append(ref)),
            )
        )
        await asyncio.wait_for(adapter.started[0].wait(), timeout=1)
        refs: list[str] = []
        second = asyncio.create_task(
            gateway.call_tool(
                scope,
                "echo",
                {},
                MCPCallCallbacks(on_created=lambda ref: refs.append(ref)),
            )
        )
        while not refs:
            await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertFalse(adapter.started[1].is_set())

        cancelled = await gateway.cancel_call_for_task(
            "task-1", refs[0], "user_cancelled"
        )
        self.assertEqual(cancelled.status.value, "cancelled")
        with self.assertRaises(asyncio.CancelledError):
            await second
        self.assertEqual(adapter.call_count, 1)

        adapter.release[0].set()
        await first
        terminal = await gateway.cancel_call_for_task(
            "task-1", first_refs[0], "late_cancel"
        )
        self.assertEqual(terminal.status.value, "already_terminal")
        await gateway.close_scope(scope, "done")
        await gateway.aclose()

    async def test_unacknowledged_legacy_cancel_closes_scope_and_reports_unknown(self) -> None:
        started = asyncio.Event()
        registered = asyncio.Event()
        refs: list[str] = []
        adapter = _BlockingCallAdapter(started)

        async def on_registered(call_ref: str) -> None:
            refs.append(call_ref)
            registered.set()

        gateway = MCPGateway(
            storage=self.storage,
            gateway_instance_id="gateway-cancel",
            credential_loader=lambda server: {},
            client_factory=lambda server, credentials: adapter,
            endpoint_revalidator=lambda server: server.endpoint_url,
            result_store=self.result_store,
            capacity=MCPTemporaryResultCapacity(
                MCPTemporaryResultCapacityConfig(4, 1),
                storage_root=self.result_store.root,
                free_bytes=lambda path: 10_000_000,
            ),
            now_fn=lambda: self.now,
        )
        scope = await gateway.open_scope(
            SimpleNamespace(username="alice"), "task-1", "server-1"
        )
        call = asyncio.create_task(
            gateway.call_tool(
                scope,
                "echo",
                {},
                MCPCallCallbacks(on_registered=on_registered),
            )
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        await asyncio.wait_for(registered.wait(), timeout=1)

        outcome = await gateway.cancel_call(scope, refs[0], "user_cancelled")

        self.assertEqual(outcome.status.value, "remote_stop_unknown")
        self.assertFalse(outcome.remote_stop_confirmed)
        self.assertTrue(adapter.cancel_notification_sent)
        self.assertTrue(adapter.closed)
        with self.assertRaises(asyncio.CancelledError):
            await call
        await gateway.aclose()
