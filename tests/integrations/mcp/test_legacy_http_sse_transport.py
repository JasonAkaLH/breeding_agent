from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import httpx

from src.integrations.mcp.client import MCPClient, MCPClientError, MCPProtocolError
from src.integrations.mcp.protocol import MCP_PROTOCOL_VERSION_2024_11_05
from src.integrations.mcp.transport_legacy_http_sse import LegacyHTTPSSETransport
from src.integrations.mcp.temporary_results import MCPTemporaryResultStore

FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "fixtures" / "mcp"


class LegacyHTTPSSETransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_post_accepts_numeric_string_response_ids(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    text="event: endpoint\ndata: /messages\n\n",
                )
            payload = json.loads(request.content.decode("utf-8"))
            if payload.get("method") == "notifications/initialized":
                return httpx.Response(204)
            result = (
                {
                    "protocolVersion": MCP_PROTOCOL_VERSION_2024_11_05,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "legacy"},
                }
                if payload.get("method") == "initialize"
                else {"tools": []}
            )
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": str(payload["id"]), "result": result},
            )

        async_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        transport = LegacyHTTPSSETransport(
            endpoint="https://legacy.example.com/sse",
            client=async_client,
        )
        client = MCPClient(
            server_id="legacy",
            transport=transport,
            protocol_version=MCP_PROTOCOL_VERSION_2024_11_05,
            transport_family="legacy_http_sse",
        )

        initialized = await client.initialize()
        tools = await client.list_tools()

        self.assertEqual(initialized["protocolVersion"], MCP_PROTOCOL_VERSION_2024_11_05)
        self.assertEqual(tools, [])
        await client.close()
        await async_client.aclose()

    async def test_2024_legacy_transport_fixtures_are_present(self) -> None:
        for relative in (
            "messages/2024-11-05/initialize_request.json",
            "messages/2024-11-05/initialize_result.json",
            "messages/2024-11-05/initialized_notification.json",
            "messages/2024-11-05/tools_list_request.json",
            "messages/2024-11-05/tools_call_request.json",
            "transports/2024-11-05/legacy_http_sse_endpoint_event.sse",
            "transports/2024-11-05/legacy_http_sse_message_response.sse",
            "transports/2024-11-05/legacy_http_sse_missing_endpoint.sse",
            "transports/2024-11-05/legacy_http_sse_persistent_response.json",
        ):
            with self.subTest(relative=relative):
                self.assertTrue((FIXTURE_ROOT / relative).exists())

    async def test_parses_endpoint_event_and_posts_jsonrpc_without_streamable_headers(self) -> None:
        seen: list[dict[str, object]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append({"method": request.method, "url": str(request.url), "headers": dict(request.headers)})
            if request.method == "GET" and request.url.path == "/sse":
                return httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    text="event: endpoint\ndata: /messages?route=legacy\n\n",
                )
            if request.method == "POST" and request.url.path == "/messages":
                payload = json.loads(request.content.decode("utf-8"))
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": {"ok": True}})
            return httpx.Response(404)

        async_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        transport = LegacyHTTPSSETransport(endpoint="https://legacy.example.com/sse", client=async_client)

        response = await transport.send(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            protocol_version=MCP_PROTOCOL_VERSION_2024_11_05,
            timeout_seconds=3,
        )
        await transport.close()
        await async_client.aclose()

        self.assertEqual(response.message["result"], {"ok": True})
        self.assertEqual([entry["method"] for entry in seen], ["GET", "POST"])
        self.assertEqual(seen[1]["url"], "https://legacy.example.com/messages?route=legacy")
        self.assertNotIn("mcp-protocol-version", seen[0]["headers"])
        self.assertNotIn("mcp-session-id", seen[1]["headers"])
        self.assertEqual(seen[0]["headers"]["accept"], "text/event-stream")

    async def test_missing_endpoint_event_fails_closed(self) -> None:
        async_client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    text='event: message\ndata: {"jsonrpc":"2.0","method":"notifications/progress","params":{}}\n\n',
                )
            )
        )
        transport = LegacyHTTPSSETransport(endpoint="https://legacy.example.com/sse", client=async_client)

        with self.assertRaises(MCPClientError) as ctx:
            await transport.send({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, protocol_version=MCP_PROTOCOL_VERSION_2024_11_05)
        await async_client.aclose()

        self.assertEqual(ctx.exception.mcp_error_code, "legacy_endpoint_missing")

    async def test_invalid_endpoint_event_fails_closed_without_raw_url_leak(self) -> None:
        raw_endpoint = "https://evil.example.com/messages?token=SECRET"
        async_client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    text=f"event: endpoint\ndata: {raw_endpoint}\n\n",
                )
            )
        )
        transport = LegacyHTTPSSETransport(endpoint="https://legacy.example.com/sse", client=async_client)

        with self.assertRaises(MCPClientError) as ctx:
            await transport.send({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, protocol_version=MCP_PROTOCOL_VERSION_2024_11_05)
        await async_client.aclose()

        self.assertEqual(ctx.exception.mcp_error_code, "legacy_endpoint_invalid")
        encoded = repr(ctx.exception.metadata) + str(ctx.exception)
        self.assertNotIn(raw_endpoint, encoded)
        self.assertNotIn("SECRET", encoded)

    async def test_post_can_return_sse_message_response(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(200, headers={"content-type": "text/event-stream"}, text="event: endpoint\ndata: /messages\n\n")
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text='event: message\ndata: {"jsonrpc":"2.0","id":7,"result":{"ok":true}}\n\n',
            )

        async_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        transport = LegacyHTTPSSETransport(endpoint="https://legacy.example.com/sse", client=async_client)

        response = await transport.send({"jsonrpc": "2.0", "id": 7, "method": "tools/list"}, protocol_version=MCP_PROTOCOL_VERSION_2024_11_05)
        await async_client.aclose()

        self.assertEqual(response.message["result"], {"ok": True})
        self.assertEqual(response.sse_events[0].event, "message")

    async def test_direct_post_response_can_stream_into_temporary_result_sink(self) -> None:
        result_bytes = b'{"content":[{"type":"text","text":"legacy streamed"}]}'

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(200, headers={"content-type": "text/event-stream"}, text="event: endpoint\ndata: /messages\n\n")
            payload = json.loads(request.content.decode("utf-8"))
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                content=b'{"jsonrpc":"2.0","id":' + str(payload["id"]).encode() + b',"result":' + result_bytes + b'}',
            )

        async_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        transport = LegacyHTTPSSETransport(endpoint="https://legacy.example.com/sse", client=async_client)
        with tempfile.TemporaryDirectory() as temporary:
            store = MCPTemporaryResultStore(Path(temporary), memory_threshold_bytes=4)
            response = await transport.send_streaming(
                {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {}},
                protocol_version=MCP_PROTOCOL_VERSION_2024_11_05,
                result_sink=store.create_sink("task-legacy"),
            )
            ref = response.message["result"]["_mcpResultRef"]["ref"]

        await transport.close()
        await async_client.aclose()

        self.assertTrue(ref.startswith("mcp-result-"))

    async def test_client_session_records_legacy_post_endpoint_after_initialize(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(200, headers={"content-type": "text/event-stream"}, text="event: endpoint\ndata: /messages\n\n")
            payload = json.loads(request.content.decode("utf-8"))
            if payload.get("method") == "initialize":
                return httpx.Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "id": payload["id"],
                        "result": {
                            "protocolVersion": MCP_PROTOCOL_VERSION_2024_11_05,
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": "legacy"},
                        },
                    },
                )
            return httpx.Response(202)

        async_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        transport = LegacyHTTPSSETransport(endpoint="https://legacy.example.com/sse", client=async_client)
        client = MCPClient(
            server_id="legacy",
            transport=transport,
            protocol_version=MCP_PROTOCOL_VERSION_2024_11_05,
            transport_family="legacy_http_sse",
        )

        await client.initialize()
        await async_client.aclose()

        self.assertEqual(client.negotiated_session.legacy_post_endpoint, "https://legacy.example.com/messages")

    async def test_endpoint_same_origin_normalizes_default_ports(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    text="event: endpoint\ndata: https://legacy.example.com/messages\n\n",
                )
            payload = json.loads(request.content.decode("utf-8"))
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": {"ok": True}})

        async_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        transport = LegacyHTTPSSETransport(endpoint="https://legacy.example.com:443/sse", client=async_client)

        response = await transport.send({"jsonrpc": "2.0", "id": 9, "method": "tools/list"}, protocol_version=MCP_PROTOCOL_VERSION_2024_11_05)
        await async_client.aclose()

        self.assertEqual(response.message["result"], {"ok": True})

    async def test_json_rpc_batch_arrays_are_rejected(self) -> None:
        transport = LegacyHTTPSSETransport(endpoint="https://legacy.example.com/sse", client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(500))))
        try:
            with self.assertRaisesRegex(MCPProtocolError, "batch"):
                await transport.send([{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}], protocol_version=MCP_PROTOCOL_VERSION_2024_11_05)  # type: ignore[arg-type]
        finally:
            await transport.close()


if __name__ == "__main__":
    unittest.main()
