from __future__ import annotations

import unittest

from src.integrations.mcp.adapter import PythonLegacyMCPClientAdapter
from src.integrations.mcp.client import MCPClientError
from src.integrations.mcp.protocol import MCPNegotiatedSession


class FakeLegacyClient:
    def __init__(self, *, fail: bool = False) -> None:
        self.closed = False
        self.fail = fail
        self.server_capabilities = {"tools": {}}
        self.negotiated_session = MCPNegotiatedSession(
            server_id="crm",
            requested_protocol_version="2025-11-25",
            negotiated_protocol_version="2025-11-25",
            transport_family="streamable_http",
            server_capabilities={"tools": {}},
            server_info={"name": "fake"},
            pinned_protocol_version=True,
            session_id="sess-1",
        )

    async def initialize(self):
        return {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake"}}

    async def list_tools(self):
        if self.fail:
            raise MCPClientError(
                "transport failed",
                code="mcp_transport_error",
                metadata={"header_value": "SECRET", "header_names": ("X-Tenant-Id",)},
            )
        return [{"name": "echo", "description": "Echo", "inputSchema": {"type": "object"}}]

    async def call_tool(self, name, arguments, **_kwargs):
        return {"content": [{"type": "text", "text": name}], "structuredContent": dict(arguments)}

    async def close(self):
        self.closed = True


class SyncCloseLegacyClient(FakeLegacyClient):
    def close(self):
        self.closed = True


class MCPAdapterContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_python_legacy_adapter_exposes_normalized_contract(self) -> None:
        client = FakeLegacyClient()
        adapter = PythonLegacyMCPClientAdapter(client)

        session = await adapter.initialize()
        tools = await adapter.list_tools()
        result = await adapter.call_tool("echo", {"x": 1})
        await adapter.close()

        self.assertEqual(session.negotiated_protocol_version, "2025-11-25")
        self.assertEqual(tools[0]["name"], "echo")
        self.assertEqual(result["structuredContent"], {"x": 1})
        self.assertTrue(client.closed)
        self.assertEqual(adapter.diagnostics(), ())

    async def test_python_legacy_adapter_accepts_sync_close_during_transition(self) -> None:
        client = SyncCloseLegacyClient()
        adapter = PythonLegacyMCPClientAdapter(client)

        await adapter.close()

        self.assertTrue(client.closed)

    async def test_python_legacy_adapter_records_redacted_error_diagnostics(self) -> None:
        adapter = PythonLegacyMCPClientAdapter(FakeLegacyClient(fail=True))

        with self.assertRaises(MCPClientError):
            await adapter.list_tools()

        diagnostics = adapter.diagnostics()
        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(diagnostics[0].error_code, "mcp_transport_error")
        self.assertEqual(diagnostics[0].metadata_keys, ("header_names",))
        encoded = repr(diagnostics[0])
        self.assertNotIn("SECRET", encoded)


if __name__ == "__main__":
    unittest.main()
