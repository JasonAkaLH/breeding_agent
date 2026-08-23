from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any

import httpx

from src.integrations.mcp.client import MCPClient, MCPClientError, MCPProtocolError
from src.integrations.mcp.config import MCPServerConfig
from src.integrations.mcp.protocol import (
    MCPCompatibilityStatus,
    MCPTransportResponse,
    is_mcp_transport_family_allowed,
    mcp_feature_status,
)
from src.integrations.mcp.runtime_state import MCPRuntimeState
from src.integrations.mcp.transport_http import StreamableHTTPTransport

STREAMABLE_2025_VERSIONS = ("2025-03-26", "2025-06-18", "2025-11-25")
FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "fixtures" / "mcp" / "messages"


class Streamable2025FakeServer:
    def __init__(
        self,
        *,
        version: str,
        tools_list_as_sse: bool = False,
        missing_header_compatible: bool = False,
        tool_call_404_once: bool = False,
        tools_response_id: str | int | None = None,
    ) -> None:
        self.version = version
        self.session_id = f"sess-{version}"
        self.tools_list_as_sse = tools_list_as_sse
        self.missing_header_compatible = missing_header_compatible
        self.tool_call_404_once = tool_call_404_once
        self.tools_response_id = tools_response_id
        self.requests: list[dict[str, Any]] = []
        self._tool_call_404_sent = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        headers = {key.lower(): value for key, value in request.headers.items()}
        payload: dict[str, Any] = {}
        if request.method == "POST":
            payload = json.loads(request.content.decode("utf-8"))
        self.requests.append({"method": request.method, "path": request.url.path, "headers": headers, "jsonrpc_method": payload.get("method")})
        if request.method == "GET":
            return httpx.Response(405)
        if request.method == "DELETE":
            return httpx.Response(405)
        if request.method != "POST":
            return httpx.Response(404)
        method = payload.get("method")
        if method != "initialize" and "mcp-protocol-version" not in headers:
            if not (self.version == "2025-03-26" and self.missing_header_compatible):
                return httpx.Response(400, json={"jsonrpc": "2.0", "id": payload.get("id"), "error": {"code": -32600, "message": "missing protocol header"}})
        if method != "initialize" and "mcp-session-id" not in headers:
            return httpx.Response(400, json={"jsonrpc": "2.0", "id": payload.get("id"), "error": {"code": -32600, "message": "missing session"}})
        if method == "initialize":
            return httpx.Response(
                200,
                headers={"MCP-Session-Id": self.session_id},
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "protocolVersion": self.version,
                        "capabilities": {"tools": {"listChanged": True}},
                        "serverInfo": {"name": f"streamable-{self.version}", "version": "1"},
                    },
                },
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/list":
            response_id = payload["id"] if self.tools_response_id is None else self.tools_response_id
            body = {
                "jsonrpc": "2.0",
                "id": response_id,
                "result": {
                    "tools": [
                        {
                            "name": "search_customer",
                            "description": "server private description must not become public",
                            "inputSchema": {"type": "object", "properties": {"keyword": {"type": "string"}}},
                            "outputSchema": {"type": "object", "properties": {"ok": {"type": "boolean"}}},
                            "annotations": {"readOnlyHint": True},
                            "title": "Server Private Title",
                            "icons": [{"src": "https://example.com/icon.png"}],
                            "tasks": {"requests": {"tools.call": {}}},
                        }
                    ]
                },
            }
            if self.tools_list_as_sse:
                return httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    text=(
                        'event: message\ndata: {"jsonrpc":"2.0","method":"notifications/progress","params":{"progress":0.5}}\n\n'
                        f"event: message\ndata: {json.dumps(body, ensure_ascii=False)}\n\n"
                    ),
                )
            return httpx.Response(200, json=body)
        if method == "tools/call":
            if self.tool_call_404_once and not self._tool_call_404_sent:
                self._tool_call_404_sent = True
                return httpx.Response(404)
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {"content": [{"type": "text", "text": f"ok {self.version}"}], "structuredContent": {"ok": True}, "isError": False},
                },
            )
        return httpx.Response(400)

    def post_request_headers(self) -> list[dict[str, str]]:
        return [entry["headers"] for entry in self.requests if entry["method"] == "POST"]


class StreamableHTTPVersionTests(unittest.IsolatedAsyncioTestCase):
    def test_streamable_http_gate_and_2025_message_fixtures_cover_three_versions(self) -> None:
        for version in STREAMABLE_2025_VERSIONS:
            with self.subTest(version=version):
                self.assertTrue(is_mcp_transport_family_allowed(version, "streamable_http"))
                for name in (
                    "initialize_request.json",
                    "initialize_result.json",
                    "initialized_notification.json",
                    "tools_list_request.json",
                    "tools_call_request.json",
                ):
                    self.assertTrue((FIXTURE_ROOT / version / name).exists(), f"missing {version}/{name}")

    async def test_client_uses_negotiated_protocol_header_and_session_for_all_2025_versions(self) -> None:
        for version in STREAMABLE_2025_VERSIONS:
            with self.subTest(version=version):
                fake = Streamable2025FakeServer(version=version, tools_list_as_sse=(version == "2025-06-18"))
                async_client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
                transport = StreamableHTTPTransport(endpoint=f"https://mcp.example.com/{version}", client=async_client)
                client = MCPClient(server_id=f"srv_{version}", transport=transport, protocol_version=version)

                init = await client.initialize()
                tools = await client.list_tools()
                result = await client.call_tool("search_customer", {"keyword": "龙粳"})
                await async_client.aclose()

                self.assertEqual(init["protocolVersion"], version)
                self.assertEqual(client.negotiated_protocol_version, version)
                self.assertEqual([tool["name"] for tool in tools], ["search_customer"])
                self.assertEqual(result["structuredContent"], {"ok": True})
                post_headers = fake.post_request_headers()
                self.assertTrue(post_headers)
                self.assertTrue(all(headers.get("mcp-protocol-version") == version for headers in post_headers))
                subsequent_headers = post_headers[1:]
                self.assertTrue(subsequent_headers)
                self.assertTrue(all(headers.get("mcp-session-id") == fake.session_id for headers in subsequent_headers))

    async def test_2025_03_client_still_sends_header_even_when_server_tolerates_missing_header(self) -> None:
        fake = Streamable2025FakeServer(version="2025-03-26", missing_header_compatible=True)
        async_client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
        transport = StreamableHTTPTransport(endpoint="https://mcp.example.com/2025-03-26", client=async_client)
        client = MCPClient(server_id="srv_2025_03", transport=transport, protocol_version="2025-03-26")

        await client.initialize()
        await client.list_tools()
        client_headers = list(fake.post_request_headers())
        manual_missing_header = await async_client.post(
            "https://mcp.example.com/2025-03-26",
            json={"jsonrpc": "2.0", "id": "manual", "method": "tools/list", "params": {}},
            headers={"Accept": "application/json, text/event-stream", "Content-Type": "application/json", "MCP-Session-Id": fake.session_id},
        )
        await async_client.aclose()

        self.assertEqual(manual_missing_header.status_code, 200)
        self.assertTrue(client_headers)
        self.assertTrue(all(headers.get("mcp-protocol-version") == "2025-03-26" for headers in client_headers))
        self.assertIsNone(fake.post_request_headers()[-1].get("mcp-protocol-version"))

    async def test_streamable_transport_rejects_json_rpc_batch_arrays(self) -> None:
        transport = StreamableHTTPTransport(endpoint="https://mcp.example.com/2025-11-25", client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(500))))
        try:
            with self.assertRaisesRegex(MCPProtocolError, "batch"):
                await transport.send([{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}], protocol_version="2025-11-25")  # type: ignore[arg-type]
        finally:
            await transport.close()

    async def test_get_stream_405_and_delete_405_are_non_fatal_and_keep_headers(self) -> None:
        for version in STREAMABLE_2025_VERSIONS:
            with self.subTest(version=version):
                fake = Streamable2025FakeServer(version=version)
                async_client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
                transport = StreamableHTTPTransport(endpoint=f"https://mcp.example.com/{version}", client=async_client)
                client = MCPClient(server_id=f"srv_{version}", transport=transport, protocol_version=version)
                await client.initialize()

                stream = await client.open_server_stream()
                deleted = await transport.delete_session(protocol_version=version, session_id=fake.session_id)
                await async_client.aclose()

                self.assertIsNone(stream.message)
                self.assertFalse(deleted)
                get_headers = [entry["headers"] for entry in fake.requests if entry["method"] == "GET"][-1]
                delete_headers = [entry["headers"] for entry in fake.requests if entry["method"] == "DELETE"][-1]
                self.assertEqual(get_headers.get("mcp-protocol-version"), version)
                self.assertEqual(get_headers.get("mcp-session-id"), fake.session_id)
                self.assertEqual(delete_headers.get("mcp-protocol-version"), version)
                self.assertEqual(delete_headers.get("mcp-session-id"), fake.session_id)

    async def test_session_404_does_not_replay_tools_call(self) -> None:
        fake = Streamable2025FakeServer(version="2025-11-25", tool_call_404_once=True)
        async_client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
        transport = StreamableHTTPTransport(endpoint="https://mcp.example.com/2025-11-25", client=async_client)
        client = MCPClient(server_id="srv_2025_11", transport=transport, protocol_version="2025-11-25")
        await client.initialize()

        with self.assertRaises(MCPClientError) as ctx:
            await client.call_tool("search_customer", {"keyword": "龙粳"})
        await async_client.aclose()

        self.assertEqual(ctx.exception.mcp_error_code, "mcp_session_expired")
        rpc_methods = [entry["jsonrpc_method"] for entry in fake.requests if entry["method"] == "POST"]
        self.assertEqual(rpc_methods, ["initialize", "notifications/initialized", "tools/call"])
        tool_call_count = sum(1 for method in rpc_methods if method == "tools/call")
        self.assertEqual(tool_call_count, 1)

    async def test_sse_response_id_mismatch_fails_closed(self) -> None:
        fake = Streamable2025FakeServer(version="2025-06-18", tools_list_as_sse=True, tools_response_id="wrong")
        async_client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
        transport = StreamableHTTPTransport(endpoint="https://mcp.example.com/2025-06-18", client=async_client)
        client = MCPClient(server_id="srv_2025_06", transport=transport, protocol_version="2025-06-18")

        with self.assertRaisesRegex(MCPProtocolError, "response id"):
            await client.list_tools()
        await async_client.aclose()

    async def test_runtime_registers_ordinary_tool_without_exposing_new_metadata(self) -> None:
        for version in STREAMABLE_2025_VERSIONS:
            with self.subTest(version=version):
                fake = Streamable2025FakeServer(version=version)
                async_client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))

                def client_factory(server: MCPServerConfig) -> MCPClient:
                    return MCPClient(
                        server_id=server.server_id,
                        transport=StreamableHTTPTransport(endpoint=server.endpoint, auth=server.auth, client=async_client),
                        protocol_version=server.protocol_version,
                        pinned_protocol_version=server.protocol_version_pinned,
                        transport_family=server.transport,
                    )

                state = MCPRuntimeState(
                    config={
                        "enabled": True,
                        "servers": [
                            {
                                "server_id": f"streamable_{version.replace('-', '_')}",
                                "transport": "streamable_http",
                                "endpoint": f"https://mcp.example.com/{version}",
                                "protocol_version": version,
                                "tools": [
                                    {
                                        "tool_name": "search_customer",
                                        "expose": True,
                                        "capability_id": f"mcp.streamable_{version.replace('-', '_')}.search_customer",
                                        "public_name": "Customer Search",
                                        "public_description": "通过 MCP 查询客户。",
                                        "risk_level": "read_only",
                                        "model_allowed_fields": ["keyword"],
                                    }
                                ],
                            }
                        ],
                    },
                    client_factory=client_factory,
                )

                refresh = await state.refresh(reason="startup", force=True)
                result = await state.call_tool(f"mcp.streamable_{version.replace('-', '_')}.search_customer", {"keyword": "龙粳", "secret": "drop"})
                await async_client.aclose()

                self.assertEqual(refresh.status, "completed")
                descriptor = state.active_bundle.descriptors[0]
                self.assertEqual(descriptor.name, "Customer Search")
                self.assertNotIn("Server Private Title", descriptor.description)
                self.assertNotIn("icon", descriptor.description.lower())
                binding = state.binding_for_capability(descriptor.capability_id)
                self.assertEqual(binding.output_schema, {"type": "object", "properties": {"ok": {"type": "boolean"}}})
                self.assertEqual(binding.model_allowed_fields, ("keyword",))
                self.assertEqual(result["structuredContent"], {"ok": True})

    def test_tasks_resources_prompts_elicitation_remain_non_public_feature_gated(self) -> None:
        for version in STREAMABLE_2025_VERSIONS:
            with self.subTest(version=version):
                self.assertEqual(mcp_feature_status(version, "tasks"), MCPCompatibilityStatus.FUTURE)
                self.assertEqual(mcp_feature_status(version, "resources"), MCPCompatibilityStatus.FUTURE)
                self.assertEqual(mcp_feature_status(version, "prompts"), MCPCompatibilityStatus.FUTURE)
                self.assertEqual(mcp_feature_status(version, "elicitation"), MCPCompatibilityStatus.FUTURE)


if __name__ == "__main__":
    unittest.main()
