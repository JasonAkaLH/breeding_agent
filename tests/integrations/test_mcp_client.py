from __future__ import annotations

import unittest

import httpx

from src.integrations.mcp.client import MCPClient, MCPProtocolError, MCPUnsupportedClientRequest
from src.integrations.mcp.protocol import MCP_PROTOCOL_VERSION, MCPTransportResponse
from src.integrations.mcp.transport_http import StreamableHTTPTransport


class RecordingTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.closed = False

    async def send(self, message, *, protocol_version, session_id=None, timeout_seconds=None, last_event_id=None):
        self.requests.append(
            {
                "message": dict(message),
                "protocol_version": protocol_version,
                "session_id": session_id,
                "timeout_seconds": timeout_seconds,
                "last_event_id": last_event_id,
            }
        )
        if not self.responses:
            return MCPTransportResponse(message=None, headers={})
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    async def close(self):
        self.closed = True


class MCPClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_initialize_sends_initialized_notification_and_minimal_capabilities(self) -> None:
        transport = RecordingTransport(
            [
                MCPTransportResponse(
                    message={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "result": {
                            "protocolVersion": MCP_PROTOCOL_VERSION,
                            "capabilities": {"tools": {"listChanged": True}},
                            "serverInfo": {"name": "fake", "version": "1"},
                        },
                    },
                    headers={"MCP-Session-Id": "sess-1"},
                ),
                MCPTransportResponse(message=None, headers={}),
            ]
        )
        client = MCPClient(server_id="crm", transport=transport, timeout_seconds=3)

        result = await client.initialize()

        self.assertEqual(result["protocolVersion"], MCP_PROTOCOL_VERSION)
        self.assertTrue(client.initialized)
        self.assertEqual(client.session_id, "sess-1")
        self.assertEqual(transport.requests[0]["message"]["method"], "initialize")
        self.assertEqual(transport.requests[0]["message"]["params"]["capabilities"], {})
        self.assertEqual(transport.requests[0]["protocol_version"], MCP_PROTOCOL_VERSION)
        self.assertIsNone(transport.requests[0]["session_id"])
        self.assertEqual(transport.requests[1]["message"]["method"], "notifications/initialized")
        self.assertNotIn("id", transport.requests[1]["message"])
        self.assertEqual(transport.requests[1]["session_id"], "sess-1")

    async def test_list_tools_paginates_and_reuses_session_header(self) -> None:
        transport = RecordingTransport(
            [
                MCPTransportResponse(
                    message={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "result": {"protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {"tools": {}}, "serverInfo": {"name": "fake"}},
                    },
                    headers={"MCP-Session-Id": "sess-2"},
                ),
                MCPTransportResponse(message=None, headers={}),
                MCPTransportResponse(
                    message={
                        "jsonrpc": "2.0",
                        "id": 2,
                        "result": {"tools": [{"name": "search_customer", "description": "search"}], "nextCursor": "n2"},
                    },
                    headers={},
                ),
                MCPTransportResponse(
                    message={
                        "jsonrpc": "2.0",
                        "id": 3,
                        "result": {"tools": [{"name": "get_customer", "description": "get"}]},
                    },
                    headers={},
                ),
            ]
        )
        client = MCPClient(server_id="crm", transport=transport)

        tools = await client.list_tools()

        self.assertEqual([tool["name"] for tool in tools], ["search_customer", "get_customer"])
        list_requests = [entry for entry in transport.requests if entry["message"].get("method") == "tools/list"]
        self.assertEqual(list_requests[0]["message"]["params"], {})
        self.assertEqual(list_requests[1]["message"]["params"], {"cursor": "n2"})
        self.assertTrue(all(entry["session_id"] == "sess-2" for entry in list_requests))
        self.assertEqual([entry["message"]["id"] for entry in list_requests], [2, 3])

    async def test_call_tool_maps_protocol_result_and_rejects_corrupt_response_id(self) -> None:
        ok_transport = RecordingTransport(
            [
                MCPTransportResponse(
                    message={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "result": {"protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {"tools": {}}, "serverInfo": {"name": "fake"}},
                    },
                    headers={},
                ),
                MCPTransportResponse(message=None, headers={}),
                MCPTransportResponse(
                    message={"jsonrpc": "2.0", "id": 2, "result": {"content": [{"type": "text", "text": "ok"}], "isError": False}},
                    headers={},
                ),
            ]
        )
        ok_client = MCPClient(server_id="crm", transport=ok_transport)
        result = await ok_client.call_tool("search_customer", {"keyword": "龙粳"})
        self.assertEqual(result["content"][0]["text"], "ok")
        self.assertEqual(ok_transport.requests[-1]["message"]["params"], {"name": "search_customer", "arguments": {"keyword": "龙粳"}})

        bad_transport = RecordingTransport(
            [
                MCPTransportResponse(
                    message={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "result": {"protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {}, "serverInfo": {"name": "fake"}},
                    },
                    headers={},
                ),
                MCPTransportResponse(message=None, headers={}),
                MCPTransportResponse(message={"jsonrpc": "2.0", "id": "wrong", "result": {}}, headers={}),
            ]
        )
        bad_client = MCPClient(server_id="crm", transport=bad_transport)
        with self.assertRaises(MCPProtocolError):
            await bad_client.call_tool("search_customer", {})

    def test_unsupported_client_feature_request_returns_json_rpc_error(self) -> None:
        response = MCPClient.unsupported_client_request_response({"jsonrpc": "2.0", "id": "r1", "method": "sampling/createMessage"})

        self.assertEqual(response["jsonrpc"], "2.0")
        self.assertEqual(response["id"], "r1")
        self.assertEqual(response["error"]["code"], MCPUnsupportedClientRequest.ERROR_CODE)
        self.assertIn("unsupported", response["error"]["message"].lower())

    async def test_streamable_http_transport_sends_headers_and_parses_sse_json_rpc(self) -> None:
        seen_headers = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen_headers.update(dict(request.headers))
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream", "MCP-Session-Id": "sess-new"},
                text='id: evt-1\nretry: 750\ndata: {"jsonrpc":"2.0","id":7,"result":{"ok":true}}\n\n',
            )

        async_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        transport = StreamableHTTPTransport(endpoint="https://mcp.example.com/rpc", client=async_client)

        response = await transport.send(
            {"jsonrpc": "2.0", "id": 7, "method": "tools/list", "params": {}},
            protocol_version=MCP_PROTOCOL_VERSION,
            session_id="sess-old",
            timeout_seconds=3,
            last_event_id="evt-0",
        )
        await transport.close()
        await async_client.aclose()

        self.assertEqual(seen_headers["mcp-protocol-version"], MCP_PROTOCOL_VERSION)
        self.assertEqual(seen_headers["mcp-session-id"], "sess-old")
        self.assertEqual(seen_headers["last-event-id"], "evt-0")
        self.assertIn("text/event-stream", seen_headers["accept"])
        self.assertEqual(response.headers["mcp-session-id"], "sess-new")
        self.assertEqual(response.last_event_id, "evt-1")
        self.assertEqual(response.sse_retry_ms, 750)
        self.assertEqual(response.message["result"], {"ok": True})
