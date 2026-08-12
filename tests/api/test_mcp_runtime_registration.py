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
                    "planner_allowed_fields": ["keyword"],
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

        self.assertIn("main_agent.respond", capabilities)
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

        self.assertIn("main_agent.respond", capability_ids)
        self.assertIn("skill.generic_data_lookup", capability_ids)
        self.assertNotIn("legacy.query", capability_ids)
        self.assertNotIn("mcp.crm.search_customer", capability_ids)

    async def test_planner_can_call_public_mcp_capability_and_main_agent_receives_result(self) -> None:
        mcp_client = FakeMCPClient()
        planner_calls = []
        main_prompts = []

        def planner(prompt, **_kwargs):
            planner_calls.append(prompt)
            return json.dumps(
                {
                    "nodes": [
                        {
                            "node_id": "lookup",
                            "capability_id": "mcp.crm.search_customer",
                            "input_payload": {"keyword": "龙粳", "token": "SECRET", "endpoint": "https://evil"},
                        }
                    ]
                },
                ensure_ascii=False,
            )

        def main_agent(prompt, **_kwargs):
            main_prompts.append(prompt)
            return "已查询到客户基础信息。"

        await self.reconfigure_runtime(
            mcp_config=MCP_CONFIG,
            mcp_client_factory=lambda server: mcp_client,
            planner_text_generator=planner,
            main_agent_stream_generator=main_agent,
        )
        response = await self.submit_message(content="查一下龙粳的客户信息", capability_id=None)
        response.raise_for_status()
        task_id = response.json()["task_id"]
        terminal = await self.wait_for_terminal_task(task_id)

        self.assertEqual(terminal["status"], "completed")
        self.assertEqual(mcp_client.calls, [("search_customer", {"keyword": "龙粳"})])
        self.assertIn("mcp.crm.search_customer", planner_calls[0])
        self.assertNotIn("endpoint", repr(mcp_client.calls))
        self.assertIn("客户基础信息：龙粳33", main_prompts[-1])
        events = await self.runtime.storage.list_events_for_task(task_id)
        self.assertIn("mcp.tool_call_started", [event.event_type for event in events])
        self.assertNotIn("SECRET", repr([event.payload for event in events]))

    async def test_runtime_shutdown_closes_mcp_clients(self) -> None:
        client = FakeMCPClient()
        await self.reconfigure_runtime(mcp_config=MCP_CONFIG, mcp_client_factory=lambda server: client)

        await self.runtime.shutdown()

        self.assertTrue(client.closed)
