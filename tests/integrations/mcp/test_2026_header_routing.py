from __future__ import annotations

import unittest

from src.integrations.mcp.adapter_2026 import MCP2026Adapter, encode_mcp_header_value
from src.integrations.mcp.protocol import MCPTransportResponse

from tests.integrations.mcp.test_2026_07_28_adapter import FakeRequestScopedTransport, response


class MCP2026HeaderRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_validated_primitive_parameters_are_mirrored_and_encoded(self) -> None:
        list_result = {
            "jsonrpc": "2.0",
            "id": 2,
            "result": {
                "resultType": "complete",
                "tools": [
                    {
                        "name": "lookup 世界",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "tenant": {"type": "string", "x-mcp-header": "Tenant"},
                                "nested": {
                                    "type": "object",
                                    "properties": {"enabled": {"type": "boolean", "x-mcp-header": "Enabled"}},
                                },
                                "count": {"type": "integer", "x-mcp-header": "Count"},
                            },
                            "required": ["tenant", "nested", "count"],
                        },
                    }
                ],
                "ttlMs": 1,
                "cacheScope": "private",
            },
        }
        call_result = {"jsonrpc": "2.0", "id": 3, "result": {"resultType": "complete", "content": []}}
        transport = FakeRequestScopedTransport(
            [response("server_discover_result.json"), MCPTransportResponse(list_result), MCPTransportResponse(call_result)]
        )
        adapter = MCP2026Adapter(server_id="crm", transport=transport)
        await adapter.initialize()
        await adapter.list_tools()

        await adapter.call_tool("lookup 世界", {"tenant": " padded ", "nested": {"enabled": True}, "count": 42})

        headers = transport.requests[-1]["request_headers"]
        self.assertEqual(headers["Mcp-Method"], "tools/call")
        self.assertEqual(headers["Mcp-Name"], encode_mcp_header_value("lookup 世界"))
        self.assertEqual(headers["Mcp-Param-Tenant"], encode_mcp_header_value(" padded "))
        self.assertEqual(headers["Mcp-Param-Enabled"], "true")
        self.assertEqual(headers["Mcp-Param-Count"], "42")

    async def test_invalid_header_annotations_exclude_only_the_bad_tool(self) -> None:
        list_result = {
            "jsonrpc": "2.0",
            "id": 2,
            "result": {
                "resultType": "complete",
                "tools": [
                    {"name": "good", "inputSchema": {"type": "object"}},
                    {
                        "name": "bad-token",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"value": {"type": "string", "x-mcp-header": "bad header"}},
                        },
                    },
                    {
                        "name": "bad-array",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "values": {
                                    "type": "array",
                                    "items": {"type": "string", "x-mcp-header": "Value"},
                                }
                            },
                        },
                    },
                    {
                        "name": "bad-number",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"value": {"type": "number", "x-mcp-header": "Value"}},
                        },
                    },
                ],
                "ttlMs": 1,
                "cacheScope": "private",
            },
        }
        transport = FakeRequestScopedTransport([response("server_discover_result.json"), MCPTransportResponse(list_result)])
        adapter = MCP2026Adapter(server_id="crm", transport=transport)
        await adapter.initialize()

        tools = await adapter.list_tools()

        self.assertEqual([tool["name"] for tool in tools], ["good"])

    def test_header_encoding_prevents_control_and_sentinel_ambiguity(self) -> None:
        self.assertEqual(encode_mcp_header_value("plain"), "plain")
        self.assertTrue(encode_mcp_header_value("line1\nline2").startswith("=?base64?"))
        self.assertTrue(encode_mcp_header_value("=?base64?literal?=").startswith("=?base64?"))


if __name__ == "__main__":
    unittest.main()
