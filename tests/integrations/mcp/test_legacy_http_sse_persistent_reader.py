from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

import httpx

from src.integrations.mcp.client import MCPClient, MCPClientError
from src.integrations.mcp.protocol import MCP_PROTOCOL_VERSION_2024_11_05
from src.integrations.mcp.transport_legacy_http_sse import LegacyHTTPSSETransport
from src.integrations.mcp.temporary_results import MCPTemporaryResultStore
from tests.integrations.mcp.legacy_sse_helpers import QueueSSEStream


class PersistentLegacyFakeServer:
    def __init__(
        self,
        *,
        emit_unknown_first: bool = False,
        never_respond: bool = False,
        stringify_response_ids: bool = False,
    ) -> None:
        self.stream = QueueSSEStream("event: endpoint\ndata: /messages?channel=abc\n\n")
        self.requests: list[dict[str, Any]] = []
        self.emit_unknown_first = emit_unknown_first
        self.never_respond = never_respond
        self.stringify_response_ids = stringify_response_ids

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append({"method": request.method, "path": request.url.path, "headers": dict(request.headers)})
        if request.method == "GET" and request.url.path == "/sse":
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=self.stream)
        if request.method != "POST" or request.url.path != "/messages":
            return httpx.Response(404)
        payload = json.loads(request.content.decode("utf-8"))
        method = payload.get("method")
        request_id = payload.get("id")
        response_id = (
            str(request_id)
            if self.stringify_response_ids and request_id is not None
            else request_id
        )
        if self.emit_unknown_first:
            self.stream.send_message({"jsonrpc": "2.0", "id": "unknown-id", "result": {"ignored": True}})
        if self.never_respond:
            return httpx.Response(202)
        if method == "initialize":
            self.stream.send_message(
                {
                    "jsonrpc": "2.0",
                    "id": response_id,
                    "result": {
                        "protocolVersion": MCP_PROTOCOL_VERSION_2024_11_05,
                        "capabilities": {"tools": {"listChanged": True}},
                        "serverInfo": {"name": "persistent-legacy", "version": "1"},
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
                    "id": response_id,
                    "result": {
                        "tools": [
                            {
                                "name": "echo",
                                "description": "Echo tool",
                                "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
                            }
                        ]
                    },
                }
            )
            return httpx.Response(202)
        if method == "tools/call":
            self.stream.send_message(
                {
                    "jsonrpc": "2.0",
                    "id": response_id,
                    "result": {"content": [{"type": "text", "text": payload["params"]["arguments"]["text"]}], "isError": False},
                }
            )
            return httpx.Response(202)
        if request_id is not None:
            self.stream.send_message({"jsonrpc": "2.0", "id": response_id, "result": {"ok": True}})
        return httpx.Response(202)


class CorrelatingLegacyFakeServer:
    def __init__(self) -> None:
        self.stream = QueueSSEStream("event: endpoint\ndata: /messages\n\n")
        self.payloads: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=self.stream)
        payload = json.loads(request.content.decode("utf-8"))
        self.payloads.append(payload)
        if len(self.payloads) == 2:
            second, first = self.payloads[1], self.payloads[0]
            self.stream.send_message({"jsonrpc": "2.0", "id": second["id"], "result": {"value": second["method"]}})
            self.stream.send_message({"jsonrpc": "2.0", "id": first["id"], "result": {"value": first["method"]}})
        return httpx.Response(202)


class ExactBufferedBeforeAliasStreamingFakeServer:
    def __init__(self) -> None:
        self.stream = QueueSSEStream("event: endpoint\ndata: /messages\n\n")
        self.payloads: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=self.stream,
            )
        self.payloads.append(json.loads(request.content.decode("utf-8")))
        if len(self.payloads) == 2:
            self.stream.send_message(
                {
                    "jsonrpc": "2.0",
                    "result": {"value": "buffered"},
                    "id": "1",
                }
            )
            self.stream.send_message(
                {
                    "jsonrpc": "2.0",
                    "result": {"value": "streamed"},
                    "id": 1,
                }
            )
        return httpx.Response(202)


class TimeoutOnPostLegacyFakeServer:
    def __init__(self) -> None:
        self.stream = QueueSSEStream("event: endpoint\ndata: /messages\n\n")

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=self.stream)
        raise httpx.TimeoutException("POST timeout", request=request)


class LegacyHTTPSSEPersistentReaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_numeric_string_ids_complete_persistent_initialize_and_list(self) -> None:
        fake = PersistentLegacyFakeServer(stringify_response_ids=True)
        async_client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
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
        self.assertEqual([tool["name"] for tool in tools], ["echo"])
        self.assertEqual(transport.unknown_response_count, 0)
        await client.close()
        await async_client.aclose()

    async def test_persistent_response_normalizes_message_and_event_id(self) -> None:
        fake = PersistentLegacyFakeServer(stringify_response_ids=True)
        async_client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
        transport = LegacyHTTPSSETransport(
            endpoint="https://legacy.example.com/sse",
            client=async_client,
        )

        response = await transport.send(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            protocol_version=MCP_PROTOCOL_VERSION_2024_11_05,
            timeout_seconds=1,
        )

        self.assertEqual(response.message["id"], 1)
        self.assertEqual(response.sse_events[-1].message["id"], 1)
        await transport.close()
        await async_client.aclose()

    async def test_client_completes_initialize_list_and_call_from_original_sse_stream(self) -> None:
        fake = PersistentLegacyFakeServer()
        async_client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
        transport = LegacyHTTPSSETransport(endpoint="https://legacy.example.com/sse", client=async_client)
        client = MCPClient(
            server_id="legacy",
            transport=transport,
            protocol_version=MCP_PROTOCOL_VERSION_2024_11_05,
            transport_family="legacy_http_sse",
        )

        init_result = await client.initialize()
        tools = await client.list_tools()
        call_result = await client.call_tool("echo", {"text": "legacy ok"})
        await client.close()
        await async_client.aclose()

        self.assertEqual(init_result["serverInfo"]["name"], "persistent-legacy")
        self.assertEqual(tools[0]["name"], "echo")
        self.assertEqual(call_result["content"][0]["text"], "legacy ok")
        self.assertEqual(sum(1 for request in fake.requests if request["method"] == "GET"), 1)
        post_headers = [request["headers"] for request in fake.requests if request["method"] == "POST"]
        self.assertTrue(post_headers)
        self.assertTrue(all("mcp-protocol-version" not in headers for headers in post_headers))
        self.assertTrue(fake.stream.closed)

    async def test_concurrent_requests_are_correlated_by_jsonrpc_id(self) -> None:
        fake = CorrelatingLegacyFakeServer()
        async_client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
        transport = LegacyHTTPSSETransport(endpoint="https://legacy.example.com/sse", client=async_client)

        first, second = await asyncio.gather(
            transport.send({"jsonrpc": "2.0", "id": "first", "method": "first"}, protocol_version=MCP_PROTOCOL_VERSION_2024_11_05, timeout_seconds=1),
            transport.send({"jsonrpc": "2.0", "id": "second", "method": "second"}, protocol_version=MCP_PROTOCOL_VERSION_2024_11_05, timeout_seconds=1),
        )
        await transport.close()
        await async_client.aclose()

        self.assertEqual(first.message["result"], {"value": "first"})
        self.assertEqual(second.message["result"], {"value": "second"})
        self.assertEqual(transport.pending_request_count, 0)

    async def test_notification_204_does_not_require_jsonrpc_response(self) -> None:
        fake = PersistentLegacyFakeServer()
        async_client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
        transport = LegacyHTTPSSETransport(endpoint="https://legacy.example.com/sse", client=async_client)

        response = await transport.send(
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            protocol_version=MCP_PROTOCOL_VERSION_2024_11_05,
            timeout_seconds=1,
        )
        await transport.close()
        await async_client.aclose()

        self.assertIsNone(response.message)
        self.assertEqual(transport.pending_request_count, 0)

    async def test_response_timeout_clears_pending_request(self) -> None:
        fake = PersistentLegacyFakeServer(never_respond=True)
        async_client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
        transport = LegacyHTTPSSETransport(endpoint="https://legacy.example.com/sse", client=async_client)

        with self.assertRaises(MCPClientError) as ctx:
            await transport.send(
                {"jsonrpc": "2.0", "id": "timeout", "method": "tools/list"},
                protocol_version=MCP_PROTOCOL_VERSION_2024_11_05,
                timeout_seconds=0.01,
            )
        await transport.close()
        await async_client.aclose()

        self.assertEqual(ctx.exception.mcp_error_code, "legacy_response_timeout")
        self.assertEqual(transport.pending_request_count, 0)

    async def test_post_timeout_raises_stable_legacy_post_failed_error(self) -> None:
        fake = TimeoutOnPostLegacyFakeServer()
        async_client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
        transport = LegacyHTTPSSETransport(endpoint="https://legacy.example.com/sse", client=async_client)

        with self.assertRaises(MCPClientError) as ctx:
            await transport.send(
                {"jsonrpc": "2.0", "id": "post-timeout", "method": "tools/list"},
                protocol_version=MCP_PROTOCOL_VERSION_2024_11_05,
                timeout_seconds=0.01,
            )
        await transport.close()
        await async_client.aclose()

        self.assertEqual(ctx.exception.mcp_error_code, "legacy_post_failed")
        self.assertEqual(transport.pending_request_count, 0)

    async def test_close_cancels_pending_request_and_reader(self) -> None:
        fake = PersistentLegacyFakeServer(never_respond=True)
        async_client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
        transport = LegacyHTTPSSETransport(endpoint="https://legacy.example.com/sse", client=async_client)
        task = asyncio.create_task(
            transport.send(
                {"jsonrpc": "2.0", "id": "will-close", "method": "tools/list"},
                protocol_version=MCP_PROTOCOL_VERSION_2024_11_05,
                timeout_seconds=10,
            )
        )
        while transport.pending_request_count == 0:
            await asyncio.sleep(0)

        await transport.close()
        with self.assertRaises(MCPClientError) as ctx:
            await task
        await async_client.aclose()

        self.assertEqual(ctx.exception.mcp_error_code, "legacy_transport_closed")
        self.assertEqual(transport.pending_request_count, 0)
        self.assertTrue(transport.reader_task_done)
        self.assertTrue(fake.stream.closed)

    async def test_unknown_stream_response_id_is_ignored_and_pending_request_times_out(self) -> None:
        fake = PersistentLegacyFakeServer(emit_unknown_first=True, never_respond=True)
        async_client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
        transport = LegacyHTTPSSETransport(endpoint="https://legacy.example.com/sse", client=async_client)

        with self.assertRaises(MCPClientError) as ctx:
            await transport.send(
                {"jsonrpc": "2.0", "id": "known", "method": "tools/list"},
                protocol_version=MCP_PROTOCOL_VERSION_2024_11_05,
                timeout_seconds=0.01,
            )
        await transport.close()
        await async_client.aclose()

        self.assertEqual(ctx.exception.mcp_error_code, "legacy_response_timeout")
        self.assertEqual(transport.unknown_response_count, 1)
        self.assertEqual(transport.pending_request_count, 0)

    async def test_streaming_request_spools_response_from_original_sse_connection(self) -> None:
        fake = PersistentLegacyFakeServer()
        async_client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
        transport = LegacyHTTPSSETransport(endpoint="https://legacy.example.com/sse", client=async_client)
        with tempfile.TemporaryDirectory() as temporary:
            store = MCPTemporaryResultStore(Path(temporary), memory_threshold_bytes=4)
            response = await transport.send_streaming(
                {
                    "jsonrpc": "2.0",
                    "id": "stream-call",
                    "method": "tools/call",
                    "params": {"name": "echo", "arguments": {"text": "legacy streamed"}},
                },
                protocol_version=MCP_PROTOCOL_VERSION_2024_11_05,
                result_sink=store.create_sink("task-legacy", scope_id="scope-legacy"),
                timeout_seconds=1,
            )
            ref_payload = response.message["result"]["_mcpResultRef"]

        await transport.close()
        await async_client.aclose()

        self.assertEqual(ref_payload["storage"], "file")
        self.assertEqual(transport.pending_request_count, 0)

    async def test_streaming_and_buffered_pending_requests_remain_correlated(self) -> None:
        fake = CorrelatingLegacyFakeServer()
        async_client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
        transport = LegacyHTTPSSETransport(endpoint="https://legacy.example.com/sse", client=async_client)
        with tempfile.TemporaryDirectory() as temporary:
            store = MCPTemporaryResultStore(Path(temporary), memory_threshold_bytes=1)
            buffered, streamed = await asyncio.gather(
                transport.send(
                    {"jsonrpc": "2.0", "id": "buffered", "method": "buffered"},
                    protocol_version=MCP_PROTOCOL_VERSION_2024_11_05,
                    timeout_seconds=1,
                ),
                transport.send_streaming(
                    {"jsonrpc": "2.0", "id": "streamed", "method": "streamed"},
                    protocol_version=MCP_PROTOCOL_VERSION_2024_11_05,
                    result_sink=store.create_sink("task", scope_id="scope"),
                    timeout_seconds=1,
                ),
            )

        await transport.close()
        await async_client.aclose()

        self.assertEqual(buffered.message["result"], {"value": "buffered"})
        self.assertTrue(streamed.message["result"]["_mcpResultRef"]["ref"].startswith("mcp-result-"))

    async def test_exact_buffered_pending_wins_before_integer_alias_streaming_sink(self) -> None:
        fake = ExactBufferedBeforeAliasStreamingFakeServer()
        async_client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
        transport = LegacyHTTPSSETransport(
            endpoint="https://legacy.example.com/sse",
            client=async_client,
        )
        with tempfile.TemporaryDirectory() as temporary:
            store = MCPTemporaryResultStore(Path(temporary), memory_threshold_bytes=1)
            streamed, buffered = await asyncio.gather(
                transport.send_streaming(
                    {"jsonrpc": "2.0", "id": 1, "method": "streamed"},
                    protocol_version=MCP_PROTOCOL_VERSION_2024_11_05,
                    result_sink=store.create_sink("task", scope_id="scope"),
                    timeout_seconds=1,
                ),
                transport.send(
                    {"jsonrpc": "2.0", "id": "1", "method": "buffered"},
                    protocol_version=MCP_PROTOCOL_VERSION_2024_11_05,
                    timeout_seconds=1,
                ),
            )

        await transport.close()
        await async_client.aclose()

        self.assertEqual(buffered.message["result"], {"value": "buffered"})
        self.assertEqual(streamed.message["id"], 1)
        self.assertIn("_mcpResultRef", streamed.message["result"])


if __name__ == "__main__":
    unittest.main()
