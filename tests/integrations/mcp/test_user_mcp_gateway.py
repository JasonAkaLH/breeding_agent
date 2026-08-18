from __future__ import annotations

import asyncio
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from src.core.enums import TaskStatus, UserMCPHealthStatus, UserMCPTransport
from src.core.models import Conversation, Task, UserMCPServer, UserMCPToolGrant
from src.integrations.mcp.client import MCPClientError
from src.integrations.mcp.endpoint_policy import (
    EndpointPolicy,
    EndpointPolicyError,
    EndpointPolicyProvenance,
)
from src.integrations.mcp.gateway import MCPCallCallbacks, MCPGateway, MCPGatewayError
from src.integrations.mcp.invalidation import (
    MCPInvalidationAction,
    MCPServerInvalidated,
)
from src.integrations.mcp.user_client import UserMCPClientFactory
from src.integrations.mcp.rollout_evidence import (
    MCPMetricErrorCategory,
    MCPMetricName,
    MCPMetricResultCategory,
    MCPMetricRoutingMode,
    MCPSafetyRedLine,
)
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


class _PublicEndpointResolver:
    def resolve(self, hostname: str, port: int):
        del hostname, port
        return ("8.8.8.8",)


_ENDPOINT_POLICY = EndpointPolicy(resolver=_PublicEndpointResolver())


def _validated_endpoint(server: UserMCPServer):
    return _ENDPOINT_POLICY.validate(server.endpoint_url)


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


class _TransientListAdapter(_Adapter):
    async def list_tools(self):
        self.list_count += 1
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


class _SafetyDetector:
    def __init__(self) -> None:
        self.violations = []

    async def report_violation(self, **kwargs) -> None:
        self.violations.append(kwargs)


class _MetricRecorder:
    def __init__(self) -> None:
        self.counts = []
        self.latencies = []
        self.gauges = []

    async def record_count(self, metric_name, **kwargs):
        self.counts.append((metric_name, kwargs))

    async def record_latency(self, metric_name, **kwargs):
        self.latencies.append((metric_name, kwargs))

    async def record_gauge(self, metric_name, **kwargs):
        self.gauges.append((metric_name, kwargs))


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

        async def client_factory(server, credentials, endpoint):
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
            endpoint_revalidator=_validated_endpoint,
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

    async def test_readonly_shadow_session_has_zero_durable_mutation(self) -> None:
        grant = UserMCPToolGrant(
            grant_id="grant-1",
            owner_user_id="alice",
            server_id="server-1",
            tool_name="echo",
            server_security_version=1,
            input_schema_sha256="existing-schema-hash",
            granted_at=self.now,
        )
        await self.storage.save_user_mcp_tool_grant(grant)
        server_before = await self.storage.get_user_mcp_server("alice", "server-1")
        grants_before = await self.storage.list_user_mcp_tool_grants(
            "alice", "server-1"
        )
        calls = {"endpoint": 0, "credentials": 0}
        adapters: list[_Adapter] = []
        validated_endpoints = []
        capacity = MCPTemporaryResultCapacity(
            MCPTemporaryResultCapacityConfig(4, 1),
            storage_root=self.result_store.root,
            free_bytes=lambda path: 10_000_000,
        )

        async def endpoint_revalidator(server):
            calls["endpoint"] += 1
            endpoint = _validated_endpoint(server)
            validated_endpoints.append(endpoint)
            return endpoint

        async def credential_loader(server):
            del server
            calls["credentials"] += 1
            self.assertEqual(
                await self.storage.list_live_user_mcp_scope_leases(now=self.now),
                [],
            )
            return {}

        async def client_factory(server, credentials, endpoint):
            del server, credentials
            adapter = _Adapter()
            adapters.append(adapter)
            return adapter

        async def readonly_client_factory(server, credentials, endpoint):
            self.assertIs(endpoint, validated_endpoints[-1])
            return await client_factory(server, credentials, endpoint)

        gateway = MCPGateway(
            storage=self.storage,
            gateway_instance_id="gateway-shadow",
            credential_loader=credential_loader,
            client_factory=client_factory,
            endpoint_revalidator=endpoint_revalidator,
            readonly_shadow_client_factory=readonly_client_factory,
            result_store=self.result_store,
            capacity=capacity,
            now_fn=lambda: self.now,
        )
        try:
            session = await gateway.open_readonly_shadow_session(
                SimpleNamespace(username="alice"), "task-1", "server-1"
            )

            self.assertEqual(session.scope.owner_user_id, "alice")
            self.assertEqual(session.scope.platform_task_id, "task-1")
            self.assertEqual(session.scope.server_id, "server-1")
            self.assertTrue(session.scope.scope_id.startswith("mcp-shadow-scope-"))
            self.assertEqual([tool.name for tool in session.catalog.tools], ["echo"])
            self.assertIs(
                session.endpoint_policy_provenance,
                EndpointPolicyProvenance.RUNTIME_ENFORCED,
            )
            self.assertFalse(hasattr(session, "call_tool"))
            self.assertEqual(calls, {"endpoint": 1, "credentials": 1})
            self.assertEqual(capacity.active_calls, 1)
            self.assertEqual(
                await self.storage.list_live_user_mcp_scope_leases(now=self.now),
                [],
            )
            self.assertEqual(
                await self.storage.get_user_mcp_server("alice", "server-1"),
                server_before,
            )
            self.assertEqual(
                await self.storage.list_user_mcp_tool_grants("alice", "server-1"),
                grants_before,
            )

            await asyncio.gather(session.aclose(), session.aclose())
            self.assertTrue(adapters[0].closed)
            self.assertEqual(capacity.active_calls, 0)
            await session.aclose()
            self.assertEqual(capacity.active_calls, 0)
        finally:
            await gateway.aclose()

    async def test_readonly_shadow_session_rejects_unverified_policy_result(
        self,
    ) -> None:
        capacity = MCPTemporaryResultCapacity(
            MCPTemporaryResultCapacityConfig(4, 1),
            storage_root=self.result_store.root,
            free_bytes=lambda path: 10_000_000,
        )
        gateway = MCPGateway(
            storage=self.storage,
            gateway_instance_id="gateway-shadow-unverified-policy",
            credential_loader=lambda server: {},
            client_factory=lambda server, credentials, endpoint: _Adapter(),
            endpoint_revalidator=lambda server: server.endpoint_url,
            readonly_shadow_client_factory=(
                lambda server, credentials, endpoint: _Adapter()
            ),
            result_store=self.result_store,
            capacity=capacity,
            now_fn=lambda: self.now,
        )
        try:
            with self.assertRaisesRegex(Exception, "endpoint_policy_rejected"):
                await gateway.open_readonly_shadow_session(
                    SimpleNamespace(username="alice"), "task-1", "server-1"
                )
            self.assertEqual(capacity.active_calls, 0)
        finally:
            await gateway.aclose()

    async def test_endpoint_rejection_marks_server_unavailable_before_credentials(self) -> None:
        credential_calls = 0

        async def credential_loader(server):
            nonlocal credential_calls
            credential_calls += 1
            return {"Authorization": "must-not-be-read"}

        async def reject_endpoint(server):
            raise EndpointPolicyError("mcp_endpoint_private_forbidden")

        gateway = MCPGateway(
            storage=self.storage,
            gateway_instance_id="gateway-private-endpoint",
            credential_loader=credential_loader,
            client_factory=lambda server, credentials, endpoint: _Adapter(),
            endpoint_revalidator=reject_endpoint,
            readonly_shadow_client_factory=(
                lambda server, credentials, endpoint: _Adapter()
            ),
            result_store=self.result_store,
            capacity=MCPTemporaryResultCapacity(
                MCPTemporaryResultCapacityConfig(4, 1),
                storage_root=self.result_store.root,
                free_bytes=lambda path: 10_000_000,
            ),
            now_fn=lambda: self.now,
        )
        try:
            with self.assertRaisesRegex(
                EndpointPolicyError,
                "mcp_endpoint_private_forbidden",
            ):
                await gateway.open_readonly_shadow_session(
                    SimpleNamespace(username="alice"), "task-1", "server-1"
                )
            server = await self.storage.get_user_mcp_server("alice", "server-1")
            self.assertIsNotNone(server)
            self.assertEqual(server.health_status, UserMCPHealthStatus.UNAVAILABLE)
            self.assertEqual(
                server.last_test_error_code,
                "mcp_endpoint_private_forbidden",
            )
            self.assertEqual(credential_calls, 0)
        finally:
            await gateway.aclose()

    async def test_readonly_shadow_factory_uses_the_exact_validated_endpoint(
        self,
    ) -> None:
        class SequenceResolver:
            def __init__(self) -> None:
                self.answers = [("8.8.8.8",), ("10.2.3.4",)]
                self.calls = 0

            def resolve(self, hostname: str, port: int):
                del hostname, port
                answer = self.answers[self.calls]
                self.calls += 1
                return answer

        class CapturingFactory(UserMCPClientFactory):
            def __init__(self, endpoint_policy) -> None:
                super().__init__(endpoint_policy)
                self.bound_endpoint = None

            def _adapter_2026(
                self,
                server,
                headers,
                endpoint,
                *,
                recovery_only=False,
            ):
                del server, headers, recovery_only
                self.bound_endpoint = endpoint
                return _Adapter()

        resolver = SequenceResolver()
        policy = EndpointPolicy(resolver=resolver)
        factory = CapturingFactory(policy)
        capacity = MCPTemporaryResultCapacity(
            MCPTemporaryResultCapacityConfig(4, 1),
            storage_root=self.result_store.root,
            free_bytes=lambda path: 10_000_000,
        )
        gateway = MCPGateway(
            storage=self.storage,
            gateway_instance_id="gateway-shadow-exact-endpoint",
            credential_loader=lambda server: {},
            client_factory=factory.create_from_validated_endpoint,
            endpoint_revalidator=factory.revalidate_endpoint,
            readonly_shadow_client_factory=factory.create_readonly_shadow,
            result_store=self.result_store,
            capacity=capacity,
            now_fn=lambda: self.now,
        )
        try:
            session = await gateway.open_readonly_shadow_session(
                SimpleNamespace(username="alice"), "task-1", "server-1"
            )

            self.assertEqual(resolver.calls, 1)
            self.assertIsNotNone(factory.bound_endpoint)
            self.assertEqual(factory.bound_endpoint.resolved_ips, ("8.8.8.8",))
            self.assertIs(
                factory.bound_endpoint.policy_provenance,
                EndpointPolicyProvenance.RUNTIME_ENFORCED,
            )
            self.assertIs(
                session.endpoint_policy_provenance,
                factory.bound_endpoint.policy_provenance,
            )
            await session.aclose()
            self.assertEqual(capacity.active_calls, 0)
        finally:
            await gateway.aclose()

    async def test_readonly_shadow_client_pins_supplied_endpoint_without_dns(
        self,
    ) -> None:
        class SequenceResolver:
            def __init__(self) -> None:
                self.answers = [("8.8.8.8",), ("10.2.3.4",)]
                self.calls = 0

            def resolve(self, hostname: str, port: int):
                del hostname, port
                answer = self.answers[self.calls]
                self.calls += 1
                return answer

        resolver = SequenceResolver()
        policy = EndpointPolicy(resolver=resolver)
        factory = UserMCPClientFactory(policy)
        server = await self.storage.get_user_mcp_server("alice", "server-1")
        self.assertIsNotNone(server)
        endpoint = policy.validate(server.endpoint_url)

        adapter = await factory.create_readonly_shadow(server, {}, endpoint)
        try:
            network_backend = (
                adapter._active._transport._client._transport._pool._network_backend
            )
            self.assertIs(network_backend._endpoint, endpoint)
            self.assertEqual(network_backend._endpoint.resolved_ips, ("8.8.8.8",))
            self.assertEqual(resolver.calls, 1)
        finally:
            await adapter.close()

    async def test_invalidation_cancels_queued_readonly_shadow_before_credentials(
        self,
    ) -> None:
        capacity = MCPTemporaryResultCapacity(
            MCPTemporaryResultCapacityConfig(1, 1),
            storage_root=self.result_store.root,
            free_bytes=lambda path: 10_000_000,
        )
        held = await capacity.acquire("holder", "held-slot")
        queued = asyncio.Event()
        calls = {"credentials": 0, "client": 0}

        async def credential_loader(server):
            del server
            calls["credentials"] += 1
            return {}

        async def readonly_client_factory(server, credentials, endpoint):
            del server, credentials, endpoint
            calls["client"] += 1
            return _Adapter()

        async def on_queued(position: int) -> None:
            del position
            queued.set()

        gateway = MCPGateway(
            storage=self.storage,
            gateway_instance_id="gateway-shadow-invalidate-queued",
            credential_loader=credential_loader,
            client_factory=lambda server, credentials, endpoint: _Adapter(),
            endpoint_revalidator=_validated_endpoint,
            readonly_shadow_client_factory=readonly_client_factory,
            result_store=self.result_store,
            capacity=capacity,
            now_fn=lambda: self.now,
        )
        opening = asyncio.create_task(
            gateway.open_readonly_shadow_session(
                SimpleNamespace(username="alice"),
                "task-1",
                "server-1",
                on_queue_entered=on_queued,
            )
        )
        try:
            await asyncio.wait_for(queued.wait(), timeout=1)
            await gateway.invalidate_server(
                MCPServerInvalidated(
                    owner_user_id="alice",
                    server_id="server-1",
                    security_version=2,
                    action=MCPInvalidationAction.SECURITY_UPDATED,
                )
            )
            with self.assertRaises(asyncio.CancelledError):
                await opening
            self.assertEqual(calls, {"credentials": 0, "client": 0})
            self.assertEqual(capacity.queued_calls, 0)
        finally:
            await held.release()
            await gateway.aclose()

    async def test_invalidation_closes_active_readonly_shadow_session(self) -> None:
        adapter = _Adapter()
        capacity = MCPTemporaryResultCapacity(
            MCPTemporaryResultCapacityConfig(1, 1),
            storage_root=self.result_store.root,
            free_bytes=lambda path: 10_000_000,
        )
        gateway = MCPGateway(
            storage=self.storage,
            gateway_instance_id="gateway-shadow-invalidate-active",
            credential_loader=lambda server: {},
            client_factory=lambda server, credentials, endpoint: adapter,
            endpoint_revalidator=_validated_endpoint,
            readonly_shadow_client_factory=(
                lambda server, credentials, endpoint: adapter
            ),
            result_store=self.result_store,
            capacity=capacity,
            now_fn=lambda: self.now,
        )
        try:
            session = await gateway.open_readonly_shadow_session(
                SimpleNamespace(username="alice"), "task-1", "server-1"
            )
            self.assertEqual(capacity.active_calls, 1)

            await gateway.invalidate_server(
                MCPServerInvalidated(
                    owner_user_id="alice",
                    server_id="server-1",
                    security_version=2,
                    action=MCPInvalidationAction.SECURITY_UPDATED,
                )
            )

            self.assertTrue(adapter.closed)
            self.assertEqual(capacity.active_calls, 0)
            await session.aclose()
        finally:
            await gateway.aclose()

    async def test_readonly_shadow_session_validates_task_owner(self) -> None:
        gateway = MCPGateway(
            storage=self.storage,
            gateway_instance_id="gateway-shadow-owner",
            credential_loader=lambda server: {},
            client_factory=lambda server, credentials, endpoint: _Adapter(),
            endpoint_revalidator=_validated_endpoint,
            result_store=self.result_store,
            capacity=MCPTemporaryResultCapacity(
                MCPTemporaryResultCapacityConfig(4, 1),
                storage_root=self.result_store.root,
                free_bytes=lambda path: 10_000_000,
            ),
            now_fn=lambda: self.now,
        )
        try:
            with self.assertRaisesRegex(Exception, "mcp_task_not_found"):
                await gateway.open_readonly_shadow_session(
                    SimpleNamespace(username="bob"), "task-1", "server-1"
                )
        finally:
            await gateway.aclose()

    async def test_close_task_cancels_readonly_shadow_opening(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        adapter = _BlockingAdapter(started, release)
        capacity = MCPTemporaryResultCapacity(
            MCPTemporaryResultCapacityConfig(4, 1),
            storage_root=self.result_store.root,
            free_bytes=lambda path: 10_000_000,
        )
        gateway = MCPGateway(
            storage=self.storage,
            gateway_instance_id="gateway-shadow-race",
            credential_loader=lambda server: {},
            client_factory=lambda server, credentials, endpoint: adapter,
            endpoint_revalidator=_validated_endpoint,
            readonly_shadow_client_factory=(
                lambda server, credentials, endpoint: adapter
            ),
            result_store=self.result_store,
            capacity=capacity,
            now_fn=lambda: self.now,
        )
        opening = asyncio.create_task(
            gateway.open_readonly_shadow_session(
                SimpleNamespace(username="alice"), "task-1", "server-1"
            )
        )
        try:
            await asyncio.wait_for(started.wait(), timeout=1)
            await gateway.close_task("task-1", "terminal")
            self.assertTrue(opening.cancelled())
            self.assertTrue(adapter.closed)
            self.assertEqual(
                await self.storage.list_live_user_mcp_scope_leases(now=self.now),
                [],
            )
            self.assertEqual(capacity.active_calls, 0)
        finally:
            release.set()
            if not opening.done():
                opening.cancel()
                await asyncio.gather(opening, return_exceptions=True)
            await gateway.aclose()

    async def test_readonly_shadow_timeout_retries_and_cleans_resources(self) -> None:
        started = asyncio.Event()
        never_release = asyncio.Event()
        adapters = [
            _BlockingAdapter(started, never_release),
            _BlockingAdapter(started, never_release),
        ]
        capacity = MCPTemporaryResultCapacity(
            MCPTemporaryResultCapacityConfig(4, 1),
            storage_root=self.result_store.root,
            free_bytes=lambda path: 10_000_000,
        )
        server_before = await self.storage.get_user_mcp_server("alice", "server-1")

        async def client_factory(server, credentials, endpoint):
            del server, credentials
            return adapters.pop(0)

        gateway = MCPGateway(
            storage=self.storage,
            gateway_instance_id="gateway-shadow-timeout",
            credential_loader=lambda server: {},
            client_factory=client_factory,
            endpoint_revalidator=_validated_endpoint,
            readonly_shadow_client_factory=(
                lambda server, credentials, endpoint: client_factory(
                    server, credentials, endpoint
                )
            ),
            result_store=self.result_store,
            capacity=capacity,
            now_fn=lambda: self.now,
            discovery_timeout_seconds=0.01,
            discovery_retry_delay_seconds=0,
        )
        created = list(adapters)
        try:
            with self.assertRaisesRegex(Exception, "discovery_timeout"):
                await gateway.open_readonly_shadow_session(
                    SimpleNamespace(username="alice"), "task-1", "server-1"
                )

            self.assertEqual([adapter.initialize_count for adapter in created], [1, 1])
            self.assertTrue(all(adapter.closed for adapter in created))
            self.assertEqual(capacity.active_calls, 0)
            self.assertEqual(
                await self.storage.list_live_user_mcp_scope_leases(now=self.now),
                [],
            )
            self.assertEqual(
                await self.storage.get_user_mcp_server("alice", "server-1"),
                server_before,
            )
            self.assertEqual(
                await self.storage.list_user_mcp_tool_grants("alice", "server-1"),
                [],
            )
        finally:
            await gateway.aclose()

    async def test_gateway_shutdown_closes_readonly_shadow_after_cleanup_failure(
        self,
    ) -> None:
        recorder = _MetricRecorder()
        adapter = _Adapter()
        capacity = MCPTemporaryResultCapacity(
            MCPTemporaryResultCapacityConfig(4, 1),
            storage_root=self.result_store.root,
            free_bytes=lambda path: 10_000_000,
        )

        async def fail_close() -> None:
            raise RuntimeError("shadow close failed")

        adapter.close = fail_close
        gateway = MCPGateway(
            storage=self.storage,
            gateway_instance_id="gateway-shadow-shutdown",
            credential_loader=lambda server: {},
            client_factory=lambda server, credentials, endpoint: adapter,
            endpoint_revalidator=_validated_endpoint,
            readonly_shadow_client_factory=(
                lambda server, credentials, endpoint: adapter
            ),
            result_store=self.result_store,
            capacity=capacity,
            now_fn=lambda: self.now,
            metric_recorder=recorder,
            metric_routing_mode=MCPMetricRoutingMode.SHADOW,
        )
        session = await gateway.open_readonly_shadow_session(
            SimpleNamespace(username="alice"), "task-1", "server-1"
        )
        self.assertEqual(capacity.active_calls, 1)

        with self.assertRaisesRegex(MCPGatewayError, "mcp_shadow_cleanup_failed"):
            await session.aclose()

        self.assertEqual(capacity.active_calls, 0)
        cleanup = [
            kwargs
            for name, kwargs in recorder.counts
            if name is MCPMetricName.RESOURCE_CLEANUP_FAILURES_TOTAL
        ]
        self.assertEqual(len(cleanup), 1)
        self.assertEqual(
            cleanup[0]["labels"].error_category,
            MCPMetricErrorCategory.CLEANUP,
        )
        self.assertEqual(
            await self.storage.list_live_user_mcp_scope_leases(now=self.now),
            [],
        )
        await gateway.aclose()

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

    async def test_persisted_assignment_guard_blocks_shadow_call_before_send(
        self,
    ) -> None:
        detector = _SafetyDetector()
        self.gateway.configure_safety_detectors(
            {MCPSafetyRedLine.SHADOW_TOOL_CALL: detector}
        )
        scope = await self.gateway.open_scope(
            SimpleNamespace(username="alice"), "task-1", "server-1"
        )

        with self.assertRaisesRegex(Exception, "mcp_shadow_tool_call_forbidden"):
            await self.gateway.call_tool(
                scope,
                "echo",
                {"text": "never sent"},
                authorization_verified=True,
            )

        self.assertEqual(self.adapters[0].call_count, 0)
        self.assertEqual(
            detector.violations,
            [{"reason_code": "shadow_call_blocked"}],
        )

    async def test_direct_call_without_authorization_records_red_line(self) -> None:
        detector = _SafetyDetector()
        self.gateway.configure_safety_detectors(
            {MCPSafetyRedLine.UNAUTHORIZED_TOOL_CALL: detector}
        )
        scope = await self.gateway.open_scope(
            SimpleNamespace(username="alice"), "task-1", "server-1"
        )

        with self.assertRaisesRegex(Exception, "mcp_tool_authorization_required"):
            await self.gateway.call_tool(scope, "echo", {"text": "never sent"})

        self.assertEqual(self.adapters[0].call_count, 0)
        self.assertEqual(
            detector.violations,
            [{"reason_code": "permission_denied_boundary"}],
        )

    async def test_runtime_metrics_cover_gateway_lifecycle_spill_and_cleanup_failure(
        self,
    ) -> None:
        recorder = _MetricRecorder()
        self.gateway.configure_rollout_metrics(
            recorder,
            MCPMetricRoutingMode.ENFORCE,
        )
        scope = await self.gateway.open_scope(
            SimpleNamespace(username="alice"), "task-1", "server-1"
        )

        await self.gateway.call_tool(scope, "echo", {"text": "large-output"})

        async def fail_close() -> None:
            raise RuntimeError("close failed")

        self.adapters[0].close = fail_close
        await self.gateway.close_scope(scope, "done")

        count_names = [name for name, _ in recorder.counts]
        latency_names = [name for name, _ in recorder.latencies]
        self.assertIn(MCPMetricName.PROTOCOL_NEGOTIATION_TOTAL, count_names)
        self.assertIn(MCPMetricName.TOOLS_LIST_ATTEMPTS_TOTAL, count_names)
        self.assertIn(
            MCPMetricName.TEMP_SPILL_BYTES,
            [name for name, _ in recorder.gauges],
        )
        self.assertIn(MCPMetricName.RESOURCE_CLEANUP_FAILURES_TOTAL, count_names)
        self.assertIn(MCPMetricName.GATEWAY_CONNECT_DURATION_SECONDS, latency_names)
        self.assertIn(MCPMetricName.TOOLS_LIST_DURATION_SECONDS, latency_names)
        self.assertEqual(
            [
                item[1]["value"]
                for item in recorder.gauges
                if item[0] is MCPMetricName.GATEWAY_ACTIVE_SCOPES
            ],
            [1, 0],
        )
        self.assertEqual(
            [
                item[1]["value"]
                for item in recorder.gauges
                if item[0] is MCPMetricName.TOOL_CALLS_ACTIVE
            ],
            [1, 0],
        )
        spill = next(
            kwargs
            for name, kwargs in recorder.gauges
            if name is MCPMetricName.TEMP_SPILL_BYTES
        )
        self.assertGreater(spill["value"], 8)
        self.assertEqual(spill["labels"].transport.value, "streamable_http")
        self.assertEqual(spill["labels"].protocol_version.value, "2025-11-25")
        await self.result_store.cleanup_task("task-1")
        spill_values = [
            kwargs["value"]
            for name, kwargs in recorder.gauges
            if name is MCPMetricName.TEMP_SPILL_BYTES
        ]
        self.assertEqual(spill_values, [spill["value"]])
        cleanup = next(
            kwargs["labels"]
            for name, kwargs in recorder.counts
            if name is MCPMetricName.RESOURCE_CLEANUP_FAILURES_TOTAL
        )
        self.assertEqual(cleanup.error_category, MCPMetricErrorCategory.CLEANUP)

    async def test_temp_spill_gauge_sums_same_dimension_scopes(self) -> None:
        recorder = _MetricRecorder()
        self.gateway.configure_rollout_metrics(
            recorder,
            MCPMetricRoutingMode.ENFORCE,
        )
        first_scope = await self.gateway.open_scope(
            SimpleNamespace(username="alice"), "task-1", "server-1"
        )
        second_scope_id = "scope-second"
        self.gateway._metric_dimension_by_scope_id[second_scope_id] = (
            self.gateway._metric_dimension_by_scope_id[first_scope.scope_id]
        )

        await self.gateway._record_temp_spill(first_scope.scope_id, 10)
        await self.gateway._record_temp_spill(second_scope_id, 20)
        await self.gateway._record_temp_spill(first_scope.scope_id, 0)
        await self.gateway._record_temp_spill(second_scope_id, 0)

        spill_values = [
            kwargs["value"]
            for name, kwargs in recorder.gauges
            if name is MCPMetricName.TEMP_SPILL_BYTES
        ]
        self.assertEqual(spill_values[-4:], [10, 30, 20, 0])

    async def test_failed_then_successful_negotiation_and_connect_are_both_counted(
        self,
    ) -> None:
        adapters = [_TransientAdapter(), _Adapter()]
        recorder = _MetricRecorder()

        async def client_factory(server, credentials, endpoint):
            del server, credentials
            return adapters.pop(0)

        gateway = MCPGateway(
            storage=self.storage,
            gateway_instance_id="gateway-metrics-retry",
            credential_loader=lambda server: {},
            client_factory=client_factory,
            endpoint_revalidator=_validated_endpoint,
            result_store=self.result_store,
            capacity=MCPTemporaryResultCapacity(
                MCPTemporaryResultCapacityConfig(4, 1),
                storage_root=self.result_store.root,
                free_bytes=lambda path: 10_000_000,
            ),
            now_fn=lambda: self.now,
            sleep=lambda _seconds: asyncio.sleep(0),
            metric_recorder=recorder,
            metric_routing_mode=MCPMetricRoutingMode.ENFORCE,
        )

        scope = await gateway.open_scope(
            SimpleNamespace(username="alice"), "task-1", "server-1"
        )

        negotiation = [
            kwargs["labels"]
            for name, kwargs in recorder.counts
            if name is MCPMetricName.PROTOCOL_NEGOTIATION_TOTAL
        ]
        self.assertEqual(
            [labels.result_category for labels in negotiation],
            [MCPMetricResultCategory.FAILED, MCPMetricResultCategory.SUCCEEDED],
        )
        self.assertEqual(
            negotiation[0].error_category,
            MCPMetricErrorCategory.TRANSPORT,
        )
        self.assertEqual(
            [
                kwargs["labels"].result_category
                for name, kwargs in recorder.latencies
                if name is MCPMetricName.GATEWAY_CONNECT_DURATION_SECONDS
            ],
            [MCPMetricResultCategory.FAILED, MCPMetricResultCategory.SUCCEEDED],
        )
        await gateway.close_scope(scope, "done")
        await gateway.aclose()

    async def test_failed_then_successful_tools_list_attempts_are_both_counted(
        self,
    ) -> None:
        adapters = [_TransientListAdapter(), _Adapter()]
        recorder = _MetricRecorder()

        async def client_factory(server, credentials, endpoint):
            del server, credentials
            return adapters.pop(0)

        gateway = MCPGateway(
            storage=self.storage,
            gateway_instance_id="gateway-tools-list-metrics-retry",
            credential_loader=lambda server: {},
            client_factory=client_factory,
            endpoint_revalidator=_validated_endpoint,
            result_store=self.result_store,
            capacity=MCPTemporaryResultCapacity(
                MCPTemporaryResultCapacityConfig(4, 1),
                storage_root=self.result_store.root,
                free_bytes=lambda path: 10_000_000,
            ),
            now_fn=lambda: self.now,
            sleep=lambda _seconds: asyncio.sleep(0),
            metric_recorder=recorder,
            metric_routing_mode=MCPMetricRoutingMode.ENFORCE,
        )

        scope = await gateway.open_scope(
            SimpleNamespace(username="alice"), "task-1", "server-1"
        )

        attempts = [
            kwargs["labels"]
            for name, kwargs in recorder.counts
            if name is MCPMetricName.TOOLS_LIST_ATTEMPTS_TOTAL
        ]
        durations = [
            kwargs["labels"]
            for name, kwargs in recorder.latencies
            if name is MCPMetricName.TOOLS_LIST_DURATION_SECONDS
        ]
        self.assertEqual(
            [labels.result_category for labels in attempts],
            [MCPMetricResultCategory.FAILED, MCPMetricResultCategory.SUCCEEDED],
        )
        self.assertEqual(
            [labels.result_category for labels in durations],
            [MCPMetricResultCategory.FAILED, MCPMetricResultCategory.SUCCEEDED],
        )
        await gateway.close_scope(scope, "done")
        await gateway.aclose()

    async def test_completed_result_survives_task_cleanup_and_store_restart(self) -> None:
        scope = await self.gateway.open_scope(
            SimpleNamespace(username="alice"), "task-1", "server-1"
        )
        outcome = await self.gateway.call_tool(scope, "echo", {"text": "hello"})
        self.assertEqual(outcome.content_type, "application/json")
        self.assertGreater(outcome.byte_size or 0, 0)
        result_ref = MCPTemporaryResultRef(
            ref=outcome.result_ref or "",
            size_bytes=outcome.byte_size or 0,
            sha256=str(outcome.result_content_sha256 or "").removeprefix("sha256:"),
            storage="file",
        )

        await self.gateway.close_scope(scope, "server_switch")

        payload = b"".join(
            [chunk async for chunk in self.result_store.iter_bytes(result_ref)]
        )
        self.assertIn(b"hello", payload)
        await self.gateway.close_task("task-1", "terminal")
        payload_after_cleanup = b"".join(
            [chunk async for chunk in self.result_store.iter_bytes(result_ref)]
        )
        self.assertEqual(payload_after_cleanup, payload)

        restarted = MCPTemporaryResultStore(
            self.result_store.root,
            memory_threshold_bytes=8,
        )
        restored = restarted.resolve_ref(result_ref.ref)
        self.assertEqual(restored.sha256, result_ref.sha256)
        payload_after_restart = b"".join(
            [chunk async for chunk in restarted.iter_bytes(restored)]
        )
        self.assertEqual(payload_after_restart, payload)

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

        async def client_factory(server, credentials, endpoint):
            nonlocal clients_created
            del server, credentials
            clients_created += 1
            return _Adapter()

        gateway = MCPGateway(
            storage=self.storage,
            gateway_instance_id="gateway-queued",
            credential_loader=credential_loader,
            client_factory=client_factory,
            endpoint_revalidator=_validated_endpoint,
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
        detector = _SafetyDetector()
        self.gateway.configure_safety_detectors(
            {MCPSafetyRedLine.CROSS_USER_ACCESS: detector}
        )
        with self.assertRaisesRegex(Exception, "mcp_task_not_found"):
            await self.gateway.open_scope(
                SimpleNamespace(username="bob"), "task-1", "server-1"
            )
        self.assertEqual(
            detector.violations,
            [{"reason_code": "task_owner_mismatch"}],
        )

    async def test_close_task_cancels_scope_that_is_still_opening(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        recorder = _MetricRecorder()

        async def client_factory(server, credentials, endpoint):
            del server, credentials
            return _BlockingAdapter(started, release)

        gateway = MCPGateway(
            storage=self.storage,
            gateway_instance_id="gateway-opening",
            credential_loader=lambda server: {},
            client_factory=client_factory,
            endpoint_revalidator=_validated_endpoint,
            result_store=self.result_store,
            capacity=MCPTemporaryResultCapacity(
                MCPTemporaryResultCapacityConfig(4, 1),
                storage_root=self.result_store.root,
                free_bytes=lambda path: 10_000_000,
            ),
            now_fn=lambda: self.now,
            metric_recorder=recorder,
            metric_routing_mode=MCPMetricRoutingMode.ENFORCE,
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
        connect = next(
            kwargs
            for name, kwargs in recorder.latencies
            if name is MCPMetricName.GATEWAY_CONNECT_DURATION_SECONDS
        )
        negotiation = next(
            kwargs
            for name, kwargs in recorder.counts
            if name is MCPMetricName.PROTOCOL_NEGOTIATION_TOTAL
        )
        self.assertEqual(
            connect["labels"].result_category,
            MCPMetricResultCategory.CANCELLED,
        )
        self.assertEqual(
            negotiation["labels"].error_category,
            MCPMetricErrorCategory.NONE,
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

        async def client_factory(server, credentials, endpoint):
            del server, credentials
            return _BlockingAdapter(started, release)

        gateway = MCPGateway(
            storage=self.storage,
            gateway_instance_id="gateway-renewing",
            credential_loader=lambda server: {},
            client_factory=client_factory,
            endpoint_revalidator=_validated_endpoint,
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

        async def client_factory(server, credentials, endpoint):
            del server, credentials
            return adapters.pop(0)

        async def record_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        gateway = MCPGateway(
            storage=self.storage,
            gateway_instance_id="gateway-retry",
            credential_loader=lambda server: {},
            client_factory=client_factory,
            endpoint_revalidator=_validated_endpoint,
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

        async def client_factory(server, credentials, endpoint):
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
            endpoint_revalidator=_validated_endpoint,
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
            client_factory=lambda server, credentials, endpoint: adapter,
            endpoint_revalidator=_validated_endpoint,
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
            client_factory=lambda server, credentials, endpoint: adapter,
            endpoint_revalidator=_validated_endpoint,
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
            client_factory=lambda server, credentials, endpoint: adapter,
            endpoint_revalidator=_validated_endpoint,
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
