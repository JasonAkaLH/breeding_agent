from __future__ import annotations

import json
import unittest

import httpx

from src.integrations.mcp.client import MCPClientError
from src.integrations.mcp.config import MCPServerConfig
from src.integrations.mcp.protocol import MCPTransportResponse
from src.integrations.mcp.runtime_state import MCPRuntimeState
from src.integrations.mcp.transport_http import StreamableHTTPTransport


class FailingClient:
    async def list_tools(self):
        raise MCPClientError("boom", code="mcp_transport_error", metadata={"secret": "HEADER-VALUE"})

    async def close(self):
        return None


class EmptyToolClient:
    server_capabilities = {"tools": {}}

    async def list_tools(self):
        return [{"name": "other_tool", "inputSchema": {"type": "object"}}]

    async def close(self):
        return None


class PlaintextHTTPSecurityTests(unittest.IsolatedAsyncioTestCase):
    def test_remote_http_endpoint_is_allowed_and_marked_plaintext(self) -> None:
        server = MCPServerConfig.from_mapping(
            {
                "server_id": "crm",
                "endpoint": "http://mcp.internal/rpc",
                "transport": "streamable_http",
                "headers": {"X-Example-Tenant": "tenant-test"},
            }
        )

        self.assertEqual(server.validation_error(), "")
        self.assertEqual(server.transport_security, "plaintext_http")
        self.assertEqual(server.request_header_names, ("X-Example-Tenant",))

    async def test_runtime_diagnostic_records_plaintext_and_header_names_without_values(self) -> None:
        state = MCPRuntimeState(
            config={
                "enabled": True,
                "servers": [
                    {
                        "server_id": "crm",
                        "endpoint": "http://mcp.internal/rpc",
                        "transport": "streamable_http",
                        "headers": {"X-Example-Tenant": "tenant-test"},
                    }
                ],
            },
            client_factory=lambda _server: FailingClient(),
        )

        result = await state.refresh(reason="startup", force=True)

        self.assertEqual(result.status, "completed")
        diagnostic = state.active_bundle.diagnostics[0]
        self.assertEqual(diagnostic.transport_security, "plaintext_http")
        self.assertEqual(diagnostic.header_names, ("X-Example-Tenant",))
        encoded = repr(diagnostic)
        self.assertNotIn("tenant-test", encoded)
        self.assertNotIn("HEADER-VALUE", encoded)

    async def test_tool_level_diagnostic_records_plaintext_and_header_names_without_values(self) -> None:
        state = MCPRuntimeState(
            config={
                "enabled": True,
                "servers": [
                    {
                        "server_id": "crm",
                        "endpoint": "http://mcp.internal/rpc",
                        "transport": "streamable_http",
                        "headers": {"X-Example-Tenant": "tenant-test"},
                        "tools": [
                            {
                                "tool_name": "missing_tool",
                                "expose": True,
                                "capability_id": "mcp.crm.missing",
                                "public_name": "Missing",
                                "public_description": "Missing test tool.",
                            }
                        ],
                    }
                ],
            },
            client_factory=lambda _server: EmptyToolClient(),
        )

        result = await state.refresh(reason="startup", force=True)

        self.assertEqual(result.status, "completed")
        diagnostic = state.active_bundle.diagnostics[0]
        self.assertEqual(diagnostic.reason, "tool_not_discovered")
        self.assertEqual(diagnostic.transport_security, "plaintext_http")
        self.assertEqual(diagnostic.header_names, ("X-Example-Tenant",))
        self.assertNotIn("tenant-test", repr(diagnostic))

    async def test_configured_headers_are_sent_by_transport(self) -> None:
        post_headers: dict[str, str] = {}
        delete_headers: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal post_headers, delete_headers
            if request.method == "DELETE":
                delete_headers = dict(request.headers)
                return httpx.Response(405)
            post_headers = dict(request.headers)
            payload = json.loads(request.content.decode("utf-8"))
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": {"ok": True}})

        async_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        transport = StreamableHTTPTransport(
            endpoint="http://mcp.internal/rpc",
            client=async_client,
            request_headers={"X-Example-Tenant": "tenant-test", "X-Example-User": "user-test"},
        )

        response = await transport.send(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            protocol_version="2025-11-25",
        )
        deleted = await transport.delete_session(protocol_version="2025-11-25", session_id="sess-1")
        await transport.close()
        await async_client.aclose()

        self.assertEqual(response.message["result"], {"ok": True})
        self.assertFalse(deleted)
        self.assertEqual(post_headers["x-example-tenant"], "tenant-test")
        self.assertEqual(post_headers["x-example-user"], "user-test")
        self.assertEqual(delete_headers["x-example-tenant"], "tenant-test")
        self.assertEqual(delete_headers["x-example-user"], "user-test")

    def test_stdio_remains_fail_closed_until_sandbox_prd(self) -> None:
        server = MCPServerConfig.from_mapping(
            {"server_id": "stdio", "endpoint": "stdio://server", "transport": "stdio", "protocol_version": "2025-11-25"}
        )

        self.assertIn("stdio MCP transport is reserved", server.validation_error())


if __name__ == "__main__":
    unittest.main()
