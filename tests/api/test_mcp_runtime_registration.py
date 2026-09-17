from __future__ import annotations

import json

from tests.api.support import APITestCase


class FakeMCPClient:
    def __init__(self, *, fail_discovery: bool = False):
        self.fail_discovery = fail_discovery
        self.calls = []
        self.list_tools_calls = 0
        self.closed = False

    async def list_tools(self):
        self.list_tools_calls += 1
        if self.fail_discovery:
            raise RuntimeError("mcp offline")
        return [
            {"name": "search_customer", "description": "server original description", "inputSchema": {"type": "object", "properties": {"keyword": {"type": "string"}}}}
        ]

    async def call_tool(self, tool_name, arguments):
        self.calls.append((tool_name, dict(arguments)))
        return {"content": [{"type": "text", "text": "客户基础信息：龙粳33"}], "structuredContent": {"name": "龙粳33"}}

    async def close(self):
        self.closed = True


MCP_CONFIG = {
    "enabled": True,
    "servers": [
        {
            "server_id": "crm",
            "endpoint": "https://mcp.example.com/rpc",
            "tools": [
                {
                    "tool_name": "search_customer",
                    "expose": True,
                    "capability_id": "mcp.crm.search_customer",
                    "public_name": "Customer Search",
                    "public_description": "通过 CRM MCP 服务查询客户基础信息。",
                    "risk_level": "read_only",
                    "model_allowed_fields": ["keyword"],
                }
            ],
        }
    ],
}


class MCPRuntimeRegistrationAPITests(APITestCase):
    async def test_startup_discovers_each_configured_legacy_server_once(self) -> None:
        client = FakeMCPClient()
        discovered_server_ids = []

        def client_factory(server):
            discovered_server_ids.append(server.server_id)
            return client

        await self.reconfigure_runtime(mcp_config=MCP_CONFIG, mcp_client_factory=client_factory)

        self.assertEqual(discovered_server_ids, ["crm"])
        self.assertEqual(client.list_tools_calls, 1)

    async def test_runtime_registers_public_mcp_capability_and_hides_server_description(self) -> None:
        client = FakeMCPClient()
        await self.reconfigure_runtime(mcp_config=MCP_CONFIG, mcp_client_factory=lambda server: client)

        response = await self.client.get("/api/v1/capabilities")
        response.raise_for_status()
        capabilities = {item["capability_id"]: item for item in response.json()["capabilities"]}

        self.assertIn("skill.generic_data_lookup", capabilities)
        self.assertNotIn("legacy.query", capabilities)
        self.assertIn("mcp.crm.search_customer", capabilities)
        mcp_descriptor = capabilities["mcp.crm.search_customer"]
        self.assertEqual(mcp_descriptor["kind"], "mcp_tool")
        self.assertEqual(mcp_descriptor["source"], "mcp")
        self.assertEqual(mcp_descriptor["name"], "Customer Search")
        self.assertNotIn("server original description", mcp_descriptor["description"])

    async def test_discovery_failure_does_not_remove_builtin_capabilities(self) -> None:
        await self.reconfigure_runtime(mcp_config=MCP_CONFIG, mcp_client_factory=lambda server: FakeMCPClient(fail_discovery=True))

        response = await self.client.get("/api/v1/capabilities")
        response.raise_for_status()
        capability_ids = {item["capability_id"] for item in response.json()["capabilities"]}

        self.assertIn("skill.generic_data_lookup", capability_ids)
        self.assertNotIn("legacy.query", capability_ids)
        self.assertNotIn("mcp.crm.search_customer", capability_ids)


    async def test_runtime_shutdown_closes_mcp_clients(self) -> None:
        client = FakeMCPClient()
        await self.reconfigure_runtime(mcp_config=MCP_CONFIG, mcp_client_factory=lambda server: client)

        await self.runtime.shutdown()

        self.assertTrue(client.closed)
