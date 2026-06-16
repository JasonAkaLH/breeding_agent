from __future__ import annotations

import unittest

from src.integrations.mcp.client import MCPClient, MCPProtocolError
from src.integrations.mcp.protocol import JSONRPC_VERSION, MCP_PROTOCOL_VERSION, MCPTransportResponse
from src.integrations.mcp.transport_http import StreamableHTTPTransport, parse_sse_events


class RecordingTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def send(self, message, *, protocol_version, session_id=None, timeout_seconds=None, last_event_id=None):
        self.requests.append({"message": dict(message), "protocol_version": protocol_version, "session_id": session_id, "last_event_id": last_event_id})
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    async def get_stream(self, *, protocol_version, session_id=None, timeout_seconds=None, last_event_id=None):
        self.requests.append({"method": "GET", "protocol_version": protocol_version, "session_id": session_id, "last_event_id": last_event_id})
        return self.responses.pop(0)

    async def close(self):
        pass


class MCPPhase2RuntimeBehaviorTests(unittest.IsolatedAsyncioTestCase):
    async def test_parse_sse_events_returns_all_non_empty_jsonrpc_messages_and_priming_metadata(self) -> None:
        events = parse_sse_events(
            ": hello\n"
            "id: prime\n"
            "retry: 500\n"
            "data:\n\n"
            "id: progress-1\n"
            "event: message\n"
            "data: {\"jsonrpc\":\"2.0\",\"method\":\"notifications/progress\",\"params\":{\"progress\":1}}\n\n"
            "id: result-1\n"
            "data: {\"jsonrpc\":\"2.0\",\"id\":2,\"result\":{\"ok\":true}}\n\n"
        )

        self.assertEqual([event.event_id for event in events], ["prime", "progress-1", "result-1"])
        self.assertTrue(events[0].is_priming)
        self.assertEqual(events[0].retry_ms, 500)
        self.assertEqual(events[1].message["method"], "notifications/progress")
        self.assertEqual(events[2].message["result"], {"ok": True})

    async def test_client_rejects_server_to_client_request_when_it_is_response_to_ordinary_request(self) -> None:
        transport = RecordingTransport(
            [
                MCPTransportResponse(message={"jsonrpc": JSONRPC_VERSION, "id": 1, "result": {"protocolVersion": MCP_PROTOCOL_VERSION}}, headers={}),
                MCPTransportResponse(message=None, headers={}),
                MCPTransportResponse(
                    message={"jsonrpc": JSONRPC_VERSION, "id": "server-req", "method": "ping", "params": {}},
                    headers={},
                ),
            ]
        )
        client = MCPClient(server_id="crm", transport=transport)

        with self.assertRaisesRegex(MCPProtocolError, "expected a JSON-RPC response"):
            await client.call_tool("search_customer", {})

    async def test_client_can_open_get_stream_with_last_event_id_after_initialize(self) -> None:
        transport = RecordingTransport(
            [
                MCPTransportResponse(message={"jsonrpc": JSONRPC_VERSION, "id": 1, "result": {"protocolVersion": MCP_PROTOCOL_VERSION}}, headers={"MCP-Session-Id": "sess-1"}, last_event_id="evt-0"),
                MCPTransportResponse(message=None, headers={}),
                MCPTransportResponse(message=None, headers={}, last_event_id="evt-5", sse_retry_ms=1000),
            ]
        )
        client = MCPClient(server_id="crm", transport=transport)
        await client.initialize()

        response = await client.open_server_stream()

        self.assertIsNone(response.message)
        self.assertEqual(response.last_event_id, "evt-5")
        self.assertEqual(transport.requests[-1], {"method": "GET", "protocol_version": MCP_PROTOCOL_VERSION, "session_id": "sess-1", "last_event_id": "evt-0"})

    async def test_session_expiry_reinitializes_before_retrying_read_request(self) -> None:
        from src.integrations.mcp.client import MCPClientError

        transport = RecordingTransport(
            [
                MCPTransportResponse(message={"jsonrpc": JSONRPC_VERSION, "id": 1, "result": {"protocolVersion": MCP_PROTOCOL_VERSION}}, headers={"MCP-Session-Id": "sess-old"}),
                MCPTransportResponse(message=None, headers={}),
                MCPClientError("expired", code="mcp_session_expired", retriable=True),
                MCPTransportResponse(message={"jsonrpc": JSONRPC_VERSION, "id": 3, "result": {"protocolVersion": MCP_PROTOCOL_VERSION}}, headers={"MCP-Session-Id": "sess-new"}),
                MCPTransportResponse(message=None, headers={}),
                MCPTransportResponse(message={"jsonrpc": JSONRPC_VERSION, "id": 4, "result": {"tools": []}}, headers={}),
            ]
        )
        client = MCPClient(server_id="crm", transport=transport)
        await client.initialize()

        tools = await client.list_tools()

        self.assertEqual(tools, [])
        self.assertEqual(client.session_id, "sess-new")
        self.assertEqual([entry["message"].get("method") for entry in transport.requests], ["initialize", "notifications/initialized", "tools/list", "initialize", "notifications/initialized", "tools/list"])


if __name__ == "__main__":
    unittest.main()
