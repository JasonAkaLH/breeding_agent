from __future__ import annotations

import json
import unittest
from dataclasses import replace

import httpx

from src.integrations.mcp.client import MCPClient
from src.integrations.mcp.config import MCPRuntimeConfig, MCPServerConfig
from src.integrations.mcp.protocol import MCP_PROTOCOL_VERSION_2024_11_05
from src.integrations.mcp.runtime_state import MCPRuntimeState, _default_client_factory
from src.integrations.mcp.transport_legacy_http_sse import LegacyHTTPSSETransport
from tests.integrations.mcp.legacy_sse_helpers import QueueSSEStream


class Legacy2024FakeServer:
    def __init__(self, *, endpoint_event: str = "/messages") -> None:
        self.endpoint_event = endpoint_event
        self.requests: list[dict[str, object]] = []
        self.stream = QueueSSEStream(f"event: endpoint\ndata: {endpoint_event}\n\n") if endpoint_event else None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append({"method": request.method, "path": request.url.path, "headers": dict(request.headers)})
        if request.method == "GET" and request.url.path == "/sse":
            if self.stream is None:
                return httpx.Response(200, headers={"content-type": "text/event-stream"}, text="event: message\ndata: {\"jsonrpc\":\"2.0\",\"method\":\"notifications/progress\",\"params\":{}}\n\n")
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=self.stream)
        if request.method != "POST" or request.url.path != "/messages":
            return httpx.Response(404)
        payload = json.loads(request.content.decode("utf-8"))
        method = payload.get("method")
        if method == "initialize":
            self.stream.send_message(
                {
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "protocolVersion": MCP_PROTOCOL_VERSION_2024_11_05,
                        "capabilities": {"tools": {"listChanged": True}},
                        "serverInfo": {"name": "legacy-fake", "version": "1"},
                    },
                }
            )
            return httpx.Response(202)
        if method == "notifications/initialized":
            return httpx.Response(204)
        if method == "tools/list":
            self.stream.send_message(
                {
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {"tools": [{"name": "search_customer", "description": "server private", "inputSchema": {"type": "object", "properties": {"keyword": {"type": "string"}}}}]},
                }
            )
            return httpx.Response(202)
        if method == "tools/call":
            self.stream.send_message({"jsonrpc": "2.0", "id": payload["id"], "result": {"content": [{"type": "text", "text": "legacy ok"}], "isError": False}})
            return httpx.Response(202)
        return httpx.Response(400)


class MCP2024LegacyRuntimeDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    def _config(self) -> MCPRuntimeConfig:
        return MCPRuntimeConfig.from_mapping(
            {
                "enabled": True,
                "servers": [
                    {
                        "server_id": "legacy_crm",
                        "transport": "legacy_http_sse",
                        "endpoint": "https://legacy.example.com/sse",
                        "protocol_version": MCP_PROTOCOL_VERSION_2024_11_05,
                        "tools": [
                            {
                                "tool_name": "search_customer",
                                "expose": True,
                                "capability_id": "mcp.legacy_crm.search_customer",
                                "public_name": "Legacy Customer Search",
                                "public_description": "通过 legacy MCP 查询客户。",
                                "risk_level": "read_only",
                                "model_allowed_fields": ["keyword"],
                            }
                        ],
                    }
                ],
            }
        )

    async def test_default_factory_selects_legacy_transport_for_2024_config(self) -> None:
        server = self._config().servers[0]
        client = _default_client_factory(3)(server)
        self.assertIsInstance(client._transport, LegacyHTTPSSETransport)
        await client.close()

    async def test_legacy_server_discovery_registers_public_tool_and_call_works(self) -> None:
        fake = Legacy2024FakeServer()
        async_client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))

        def client_factory(server: MCPServerConfig) -> MCPClient:
            return MCPClient(
                server_id=server.server_id,
                transport=LegacyHTTPSSETransport(endpoint=server.endpoint, auth=server.auth, client=async_client),
                protocol_version=server.protocol_version,
                pinned_protocol_version=server.protocol_version_pinned,
                transport_family=server.transport,
            )

        state = MCPRuntimeState(config=self._config(), client_factory=client_factory)
        refresh = await state.refresh(reason="startup", force=True)

        self.assertEqual(refresh.status, "completed")
        self.assertIn("mcp.legacy_crm.search_customer", state.active_mcp_capability_ids())
        descriptor = state.active_bundle.descriptors[0]
        self.assertEqual(descriptor.name, "Legacy Customer Search")
        self.assertNotIn("server private", descriptor.description)

        result = await state.call_tool("mcp.legacy_crm.search_customer", {"keyword": "龙粳", "secret": "drop"})

        self.assertEqual(result["content"][0]["text"], "legacy ok")
        post_headers = [entry["headers"] for entry in fake.requests if entry["method"] == "POST"]
        self.assertTrue(post_headers)
        self.assertTrue(all("mcp-protocol-version" not in headers for headers in post_headers))
        await async_client.aclose()

    async def test_optional_missing_endpoint_records_legacy_reason_and_required_fails(self) -> None:
        optional_fake = Legacy2024FakeServer(endpoint_event="")
        optional_http = httpx.AsyncClient(transport=httpx.MockTransport(optional_fake.handler))

        def optional_factory(server: MCPServerConfig) -> MCPClient:
            return MCPClient(
                server_id=server.server_id,
                transport=LegacyHTTPSSETransport(endpoint=server.endpoint, auth=server.auth, client=optional_http),
                protocol_version=server.protocol_version,
                pinned_protocol_version=server.protocol_version_pinned,
                transport_family=server.transport,
            )

        optional_state = MCPRuntimeState(config=self._config(), client_factory=optional_factory)
        optional_result = await optional_state.refresh(reason="startup", force=True)
        self.assertEqual(optional_result.status, "completed")
        self.assertEqual(optional_state.active_bundle.diagnostics[0].reason, "legacy_endpoint_missing")

        required_payload = self._config()
        required_config = MCPRuntimeConfig(enabled=True, servers=(replace(required_payload.servers[0], required=True),))
        required_fake = Legacy2024FakeServer(endpoint_event="")
        required_http = httpx.AsyncClient(transport=httpx.MockTransport(required_fake.handler))

        def required_factory(server: MCPServerConfig) -> MCPClient:
            return MCPClient(
                server_id=server.server_id,
                transport=LegacyHTTPSSETransport(endpoint=server.endpoint, auth=server.auth, client=required_http),
                protocol_version=server.protocol_version,
                pinned_protocol_version=server.protocol_version_pinned,
                transport_family=server.transport,
            )

        required_state = MCPRuntimeState(config=required_config, client_factory=required_factory)
        required_pending = await required_state.prepare_refresh(reason="startup", force=True)
        self.assertEqual(required_pending.result.status, "failed")
        self.assertEqual(required_pending.bundle.diagnostics[0].reason, "legacy_endpoint_missing")
        await optional_http.aclose()
        await required_http.aclose()


if __name__ == "__main__":
    unittest.main()
