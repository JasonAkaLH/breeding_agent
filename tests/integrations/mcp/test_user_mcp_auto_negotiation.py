from __future__ import annotations

import asyncio
import unittest
from dataclasses import replace
from typing import Any

from src.core.enums import UserMCPProtocolPreference, UserMCPTransport
from src.core.models import UserMCPServer
from src.integrations.mcp.adapter_2025_tasks import MCP2025TasksAdapter
from src.integrations.mcp.adapter_2026 import (
    MCPMethodNotFoundError,
    MCPUnsupportedProtocolVersionError,
)
from src.integrations.mcp.client import (
    MCPAuthRequiredError,
    MCPClient,
    MCPClientError,
    MCPProtocolError,
    MCPRemoteError,
)
from src.integrations.mcp.endpoint_policy import EndpointPolicy
from src.integrations.mcp.protocol import MCPNegotiatedSession, MCPTransportResponse
from src.integrations.mcp.user_client import (
    UserMCPClientFactory,
    _AutoNegotiatingAdapter,
)


class _Resolver:
    def resolve(self, _hostname: str, _port: int) -> tuple[str, ...]:
        return ("8.8.8.8",)


def _session(
    version: str,
    *,
    requested_version: str | None = None,
    pinned: bool = False,
) -> MCPNegotiatedSession:
    return MCPNegotiatedSession(
        server_id="server-a",
        requested_protocol_version=requested_version or version,
        negotiated_protocol_version=version,
        transport_family="streamable_http",
        server_capabilities={"tools": {}},
        server_info={"name": "fixture"},
        pinned_protocol_version=pinned,
    )


class _FakeAdapter:
    def __init__(
        self,
        *,
        session: MCPNegotiatedSession | None = None,
        initialize_error: BaseException | None = None,
        close_error: BaseException | None = None,
        list_error: BaseException | None = None,
        call_error: BaseException | None = None,
        label: str = "adapter",
    ) -> None:
        self.negotiated_session = session
        self.server_capabilities = {"adapter": label}
        self.initialize_error = initialize_error
        self.close_error = close_error
        self.list_error = list_error
        self.call_error = call_error
        self.initialize_count = 0
        self.close_count = 0
        self.list_count = 0
        self.call_count = 0
        self.cancel_count = 0

    async def initialize(self) -> MCPNegotiatedSession | None:
        self.initialize_count += 1
        if self.initialize_error is not None:
            raise self.initialize_error
        return self.negotiated_session

    async def list_tools(self) -> list[dict[str, Any]]:
        self.list_count += 1
        if self.list_error is not None:
            raise self.list_error
        return [{"name": "fixture"}]

    async def call_tool(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        self.call_count += 1
        if self.call_error is not None:
            raise self.call_error
        return {"content": []}

    async def cancel_request(self, request_id: Any, *, reason: str = "") -> bool:
        del request_id, reason
        self.cancel_count += 1
        return True

    async def close(self) -> None:
        self.close_count += 1
        if self.close_error is not None:
            raise self.close_error


class _InitializeTransport:
    def __init__(self, negotiated_version: str) -> None:
        self.negotiated_version = negotiated_version
        self.requests: list[tuple[dict[str, Any], str]] = []

    async def send(
        self,
        message: dict[str, Any],
        *,
        protocol_version: str,
        session_id: str | None = None,
        timeout_seconds: float | None = None,
        last_event_id: str | None = None,
    ) -> MCPTransportResponse:
        del session_id, timeout_seconds, last_event_id
        self.requests.append((dict(message), protocol_version))
        if message.get("method") == "initialize":
            return MCPTransportResponse(
                message={
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "result": {
                        "protocolVersion": self.negotiated_version,
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "fixture", "version": "1"},
                    },
                }
            )
        return MCPTransportResponse(message=None)

    async def close(self) -> None:
        return None


class _CapturingFactory(UserMCPClientFactory):
    def __init__(
        self,
        modern: _FakeAdapter,
        legacy: _FakeAdapter,
    ) -> None:
        super().__init__(EndpointPolicy(resolver=_Resolver()))
        self.modern = modern
        self.legacy = legacy
        self.legacy_calls: list[tuple[str, bool, bool]] = []

    def _adapter_2026(self, *args: Any, **kwargs: Any) -> _FakeAdapter:
        del args, kwargs
        return self.modern

    def _legacy_adapter(
        self,
        server: UserMCPServer,
        headers: dict[str, str],
        endpoint: Any,
        version: str,
        *,
        pinned_protocol_version: bool = True,
        wrap_2025_tasks: bool = True,
    ) -> _FakeAdapter:
        del server, headers, endpoint
        self.legacy_calls.append(
            (version, pinned_protocol_version, wrap_2025_tasks)
        )
        return self.legacy


class AutoNegotiatingAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_unpinned_2025_11_initialize_accepts_supported_earlier_version(
        self,
    ) -> None:
        for version in ("2025-06-18", "2025-03-26"):
            with self.subTest(version=version):
                transport = _InitializeTransport(version)
                client = MCPClient(
                    server_id="server-a",
                    transport=transport,
                    protocol_version="2025-11-25",
                    pinned_protocol_version=False,
                    transport_family="streamable_http",
                )

                result = await client.initialize()
                session = client.negotiated_session

                self.assertEqual(result["protocolVersion"], version)
                self.assertIsNotNone(session)
                assert session is not None
                self.assertEqual(session.requested_protocol_version, "2025-11-25")
                self.assertEqual(session.negotiated_protocol_version, version)
                self.assertFalse(session.pinned_protocol_version)
                self.assertEqual(
                    [request[1] for request in transport.requests],
                    ["2025-11-25", version],
                )

    async def test_modern_success_keeps_initial_adapter(self) -> None:
        modern = _FakeAdapter(session=_session("2026-07-28"), label="modern")
        legacy_calls = 0

        def legacy_factory(_version: str) -> _FakeAdapter:
            nonlocal legacy_calls
            legacy_calls += 1
            return _FakeAdapter(session=_session("2025-11-25"))

        adapter = _AutoNegotiatingAdapter(
            initial=modern,
            legacy_factory=legacy_factory,
            tasks_adapter_factory=lambda client: client,
        )

        self.assertEqual(await adapter.initialize(), modern.negotiated_session)
        self.assertEqual(legacy_calls, 0)
        self.assertIs(adapter.negotiated_session, modern.negotiated_session)
        self.assertEqual(await adapter.list_tools(), [{"name": "fixture"}])
        self.assertEqual(modern.list_count, 1)

    async def test_protocol_and_remote_initialize_errors_fallback_once(self) -> None:
        errors = (
            MCPProtocolError("malformed initialize"),
            MCPRemoteError("remote initialize failure"),
            MCPUnsupportedProtocolVersionError(
                supported_versions=("2025-11-25",),
                requested_version="2026-07-28",
                request_method="server/discover",
            ),
            MCPMethodNotFoundError(
                "server/discover unavailable",
                request_method="server/discover",
            ),
        )
        for error in errors:
            with self.subTest(error=type(error).__name__):
                modern = _FakeAdapter(initialize_error=error)
                legacy = _FakeAdapter(
                    session=_session(
                        "2025-06-18",
                        requested_version="2025-11-25",
                    )
                )
                requested_versions: list[str] = []
                adapter = _AutoNegotiatingAdapter(
                    initial=modern,
                    legacy_factory=lambda version: (
                        requested_versions.append(version) or legacy
                    ),
                    tasks_adapter_factory=lambda client: client,
                )

                self.assertEqual(await adapter.initialize(), legacy.negotiated_session)
                self.assertEqual(requested_versions, ["2025-11-25"])
                self.assertEqual(modern.close_count, 1)
                self.assertEqual(legacy.initialize_count, 1)
                self.assertIs(adapter.negotiated_session, legacy.negotiated_session)

    async def test_http_handshake_rejections_fallback_once(self) -> None:
        for status_code in (400, 404, 405):
            with self.subTest(status_code=status_code):
                modern = _FakeAdapter(
                    initialize_error=MCPClientError(
                        "modern HTTP handshake rejected",
                        code="mcp_http_error",
                        retriable=False,
                        metadata={"status_code": status_code},
                    )
                )
                legacy = _FakeAdapter(
                    session=_session(
                        "2025-11-25",
                        requested_version="2025-11-25",
                    )
                )
                requested_versions: list[str] = []
                adapter = _AutoNegotiatingAdapter(
                    initial=modern,
                    legacy_factory=lambda version: (
                        requested_versions.append(version) or legacy
                    ),
                    tasks_adapter_factory=lambda client: client,
                )

                self.assertIs(await adapter.initialize(), legacy.negotiated_session)
                self.assertEqual(requested_versions, ["2025-11-25"])
                self.assertEqual(modern.initialize_count, 1)
                self.assertEqual(modern.close_count, 1)
                self.assertEqual(legacy.initialize_count, 1)

    async def test_non_protocol_initialize_errors_do_not_fallback(self) -> None:
        error_factories = (
            MCPAuthRequiredError,
            lambda: MCPClientError(
                "network",
                code="mcp_transport_error",
                retriable=True,
            ),
            lambda: MCPClientError(
                "rate limited",
                code="mcp_http_error",
                retriable=False,
                metadata={"status_code": 429},
            ),
            lambda: MCPClientError(
                "server failed",
                code="mcp_http_error",
                retriable=True,
                metadata={"status_code": 500},
            ),
            lambda: MCPClientError(
                "string status",
                code="mcp_http_error",
                retriable=False,
                metadata={"status_code": "400"},
            ),
            lambda: MCPClientError(
                "boolean status",
                code="mcp_http_error",
                retriable=False,
                metadata={"status_code": True},
            ),
            lambda: TimeoutError("timeout"),
            asyncio.CancelledError,
            lambda: ValueError("local bug"),
        )
        for error_factory in error_factories:
            error = error_factory()
            with self.subTest(error=type(error).__name__):
                modern = _FakeAdapter(initialize_error=error)
                legacy_calls = 0

                def legacy_factory(_version: str) -> _FakeAdapter:
                    nonlocal legacy_calls
                    legacy_calls += 1
                    return _FakeAdapter()

                adapter = _AutoNegotiatingAdapter(
                    initial=modern,
                    legacy_factory=legacy_factory,
                    tasks_adapter_factory=lambda client: client,
                )
                with self.assertRaises(type(error)):
                    await adapter.initialize()
                self.assertEqual(legacy_calls, 0)
                self.assertEqual(modern.close_count, 0)

    async def test_close_failure_stops_before_legacy_candidate(self) -> None:
        modern = _FakeAdapter(
            initialize_error=MCPProtocolError("malformed initialize"),
            close_error=RuntimeError("close failed"),
        )
        legacy_calls = 0

        def legacy_factory(_version: str) -> _FakeAdapter:
            nonlocal legacy_calls
            legacy_calls += 1
            return _FakeAdapter()

        adapter = _AutoNegotiatingAdapter(
            initial=modern,
            legacy_factory=legacy_factory,
            tasks_adapter_factory=lambda client: client,
        )
        with self.assertRaisesRegex(RuntimeError, "close failed"):
            await adapter.initialize()
        self.assertEqual(modern.close_count, 1)
        self.assertEqual(legacy_calls, 0)

    async def test_legacy_failure_is_returned_without_third_candidate(self) -> None:
        modern = _FakeAdapter(
            initialize_error=MCPProtocolError("malformed initialize")
        )
        legacy = _FakeAdapter(
            initialize_error=MCPRemoteError("legacy initialize failed")
        )
        legacy_calls = 0

        def legacy_factory(_version: str) -> _FakeAdapter:
            nonlocal legacy_calls
            legacy_calls += 1
            return legacy

        adapter = _AutoNegotiatingAdapter(
            initial=modern,
            legacy_factory=legacy_factory,
            tasks_adapter_factory=lambda client: client,
        )
        with self.assertRaisesRegex(MCPRemoteError, "legacy initialize failed"):
            await adapter.initialize()
        self.assertEqual(legacy_calls, 1)
        self.assertEqual(legacy.initialize_count, 1)

    async def test_tool_failures_after_success_do_not_switch(self) -> None:
        list_error = MCPRemoteError("tools/list failed")
        call_error = MCPProtocolError("tools/call failed")
        modern = _FakeAdapter(
            session=_session("2026-07-28"),
            list_error=list_error,
            call_error=call_error,
        )
        legacy_calls = 0

        def legacy_factory(_version: str) -> _FakeAdapter:
            nonlocal legacy_calls
            legacy_calls += 1
            return _FakeAdapter()

        adapter = _AutoNegotiatingAdapter(
            initial=modern,
            legacy_factory=legacy_factory,
            tasks_adapter_factory=lambda client: client,
        )
        await adapter.initialize()
        with self.assertRaisesRegex(MCPRemoteError, "tools/list failed"):
            await adapter.list_tools()
        with self.assertRaisesRegex(MCPProtocolError, "tools/call failed"):
            await adapter.call_tool("fixture", {})
        self.assertEqual(legacy_calls, 0)
        self.assertEqual(modern.close_count, 0)

    async def test_actual_2025_11_session_enables_tasks_wrapper(self) -> None:
        modern = _FakeAdapter(
            initialize_error=MCPProtocolError("malformed initialize")
        )
        legacy = _FakeAdapter(
            session=_session(
                "2025-11-25",
                requested_version="2025-11-25",
            ),
            label="legacy",
        )
        wrapped = _FakeAdapter(
            session=legacy.negotiated_session,
            label="tasks",
        )
        wrapped_clients: list[_FakeAdapter] = []
        adapter = _AutoNegotiatingAdapter(
            initial=modern,
            legacy_factory=lambda _version: legacy,
            tasks_adapter_factory=lambda client: (
                wrapped_clients.append(client) or wrapped
            ),
        )

        session = await adapter.initialize()

        self.assertIs(session, legacy.negotiated_session)
        self.assertEqual(wrapped_clients, [legacy])
        self.assertIs(adapter.negotiated_session, wrapped.negotiated_session)
        self.assertEqual(adapter.server_capabilities, {"adapter": "tasks"})
        await adapter.call_tool("fixture", {})
        await adapter.cancel_request("request-1", reason="test")
        await adapter.close()
        self.assertEqual(wrapped.call_count, 1)
        self.assertEqual(wrapped.cancel_count, 1)
        self.assertEqual(wrapped.close_count, 1)

    async def test_earlier_negotiated_session_keeps_base_adapter(self) -> None:
        for version in ("2025-06-18", "2025-03-26"):
            with self.subTest(version=version):
                modern = _FakeAdapter(
                    initialize_error=MCPRemoteError("modern initialize failed")
                )
                legacy = _FakeAdapter(
                    session=_session(
                        version,
                        requested_version="2025-11-25",
                    ),
                    label="legacy",
                )
                wrapper_calls = 0

                def wrap_tasks(_client: Any) -> Any:
                    nonlocal wrapper_calls
                    wrapper_calls += 1
                    return _FakeAdapter(label="tasks")

                adapter = _AutoNegotiatingAdapter(
                    initial=modern,
                    legacy_factory=lambda _version: legacy,
                    tasks_adapter_factory=wrap_tasks,
                )
                self.assertIs(await adapter.initialize(), legacy.negotiated_session)
                self.assertEqual(wrapper_calls, 0)
                self.assertIs(adapter.negotiated_session, legacy.negotiated_session)
                self.assertEqual(adapter.server_capabilities, {"adapter": "legacy"})


class UserMCPClientFactoryAutoNegotiationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.server = UserMCPServer(
            server_id="server-a",
            owner_user_id="alice",
            display_name="Server",
            routing_description="Fixture server",
            endpoint_url="https://example.test/mcp",
            transport=UserMCPTransport.STREAMABLE_HTTP,
            protocol_preference=UserMCPProtocolPreference.AUTO,
        )

    async def test_streamable_auto_requests_unpinned_2025_11_candidate(self) -> None:
        modern = _FakeAdapter(
            initialize_error=MCPProtocolError("modern initialize failed")
        )
        legacy = _FakeAdapter(
            session=_session(
                "2025-06-18",
                requested_version="2025-11-25",
            )
        )
        factory = _CapturingFactory(modern, legacy)
        endpoint = await factory.revalidate_endpoint(self.server)
        adapter = factory.create_from_validated_endpoint(self.server, {}, endpoint)

        await adapter.initialize()

        self.assertEqual(factory.legacy_calls, [("2025-11-25", False, False)])
        self.assertNotIsInstance(adapter._active, MCP2025TasksAdapter)
        await adapter.close()

    async def test_streamable_auto_wraps_only_actual_2025_11_session(self) -> None:
        modern = _FakeAdapter(
            initialize_error=MCPProtocolError("modern initialize failed")
        )
        legacy = _FakeAdapter(
            session=_session(
                "2025-11-25",
                requested_version="2025-11-25",
            )
        )
        factory = _CapturingFactory(modern, legacy)
        endpoint = await factory.revalidate_endpoint(self.server)
        adapter = factory.create_from_validated_endpoint(self.server, {}, endpoint)

        session = await adapter.initialize()

        self.assertIs(session, legacy.negotiated_session)
        self.assertEqual(factory.legacy_calls, [("2025-11-25", False, False)])
        self.assertIsInstance(adapter._active, MCP2025TasksAdapter)
        self.assertIs(adapter.negotiated_session, legacy.negotiated_session)
        await adapter.close()

    async def test_explicit_protocols_and_legacy_auto_remain_pinned(self) -> None:
        modern = _FakeAdapter(session=_session("2026-07-28"))
        legacy = _FakeAdapter()
        factory = _CapturingFactory(modern, legacy)

        for preference in (
            UserMCPProtocolPreference.V2025_03_26,
            UserMCPProtocolPreference.V2025_06_18,
            UserMCPProtocolPreference.V2025_11_25,
        ):
            server = replace(self.server, protocol_preference=preference)
            endpoint = await factory.revalidate_endpoint(server)
            self.assertIs(
                factory.create_from_validated_endpoint(server, {}, endpoint),
                legacy,
            )

        legacy_auto = replace(
            self.server,
            transport=UserMCPTransport.LEGACY_HTTP_SSE,
        )
        endpoint = await factory.revalidate_endpoint(legacy_auto)
        self.assertIs(
            factory.create_from_validated_endpoint(legacy_auto, {}, endpoint),
            legacy,
        )

        explicit_2026 = replace(
            self.server,
            protocol_preference=UserMCPProtocolPreference.V2026_07_28,
        )
        endpoint = await factory.revalidate_endpoint(explicit_2026)
        self.assertIs(
            factory.create_from_validated_endpoint(explicit_2026, {}, endpoint),
            modern,
        )

        self.assertEqual(
            factory.legacy_calls,
            [
                ("2025-03-26", True, True),
                ("2025-06-18", True, True),
                ("2025-11-25", True, True),
                ("2024-11-05", True, True),
            ],
        )


if __name__ == "__main__":
    unittest.main()
