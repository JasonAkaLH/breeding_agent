from __future__ import annotations

import unittest

from src.integrations.mcp.config import MCPRuntimeConfig
from src.integrations.mcp.runtime_state import MCPRuntimeState


class FakeClient:
    def __init__(self, *, tools=None, fail=False):
        self.tools = list(tools or [])
        self.fail = fail
        self.calls = []
        self.closed = False

    async def list_tools(self):
        if self.fail:
            raise RuntimeError("discovery unavailable")
        return self.tools

    async def call_tool(self, tool_name, arguments):
        self.calls.append((tool_name, dict(arguments)))
        return {"content": [{"type": "text", "text": "called"}], "structuredContent": {"ok": True}}

    async def close(self):
        self.closed = True


class MCPRuntimeStateTests(unittest.IsolatedAsyncioTestCase):
    def _config(self):
        return MCPRuntimeConfig.from_mapping(
            {
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
                            },
                            {"tool_name": "hidden_tool", "expose": False},
                        ],
                    }
                ],
            }
        )

    async def test_refresh_builds_public_bundle_from_allowlisted_readonly_tool(self) -> None:
        client = FakeClient(
            tools=[
                {"name": "search_customer", "description": "server text", "inputSchema": {"type": "object", "properties": {"keyword": {"type": "string"}}}},
                {"name": "hidden_tool", "description": "hidden"},
            ]
        )
        state = MCPRuntimeState(config=self._config(), client_factory=lambda server: client, reserved_capability_ids=("main_agent.respond",))

        result = await state.refresh(reason="startup", force=True)

        self.assertEqual(result.status, "completed")
        bundle = state.active_bundle
        self.assertEqual([descriptor.capability_id for descriptor in bundle.descriptors], ["mcp.crm.search_customer"])
        descriptor = bundle.descriptors[0]
        self.assertEqual(descriptor.kind, "mcp_tool")
        self.assertEqual(descriptor.source, "mcp")
        self.assertEqual(descriptor.name, "Customer Search")
        self.assertNotIn("server text", descriptor.description)
        self.assertEqual(bundle.payload_policies["mcp.crm.search_customer"].planner_allowed_fields, ("keyword",))
        binding = state.binding_for_capability("mcp.crm.search_customer")
        self.assertEqual(binding.server_id, "crm")
        self.assertEqual(binding.tool_name, "search_customer")

    async def test_duplicate_capability_id_is_skipped_and_diagnostic_is_kept(self) -> None:
        client = FakeClient(tools=[{"name": "search_customer", "inputSchema": {"type": "object"}}])
        state = MCPRuntimeState(config=self._config(), client_factory=lambda server: client, reserved_capability_ids=("mcp.crm.search_customer",))

        result = await state.refresh(reason="startup", force=True)

        self.assertEqual(result.status, "completed")
        self.assertEqual(state.active_bundle.descriptors, ())
        self.assertEqual(result.skipped_count, 1)
        self.assertEqual(state.active_bundle.diagnostics[0].reason, "reserved_or_duplicate_capability_id")

    async def test_optional_discovery_failure_keeps_previous_active_bundle(self) -> None:
        first_client = FakeClient(tools=[{"name": "search_customer", "inputSchema": {"type": "object"}}])
        failing_client = FakeClient(fail=True)
        clients = iter([first_client, failing_client])
        state = MCPRuntimeState(config=self._config(), client_factory=lambda server: next(clients), reserved_capability_ids=())
        first = await state.refresh(reason="startup", force=True)
        first_revision = state.active_revision

        second = await state.refresh(reason="manual", force=True)

        self.assertEqual(first.status, "completed")
        self.assertEqual(second.status, "failed")
        self.assertEqual(state.active_revision, first_revision)
        self.assertEqual([descriptor.capability_id for descriptor in state.active_bundle.descriptors], ["mcp.crm.search_customer"])

    async def test_public_tool_with_input_fields_requires_explicit_planner_allowlist(self) -> None:
        config = MCPRuntimeConfig.from_mapping(
            {
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
                                "public_description": "查询客户。",
                                "risk_level": "read_only",
                            }
                        ],
                    }
                ],
            }
        )
        client = FakeClient(tools=[{"name": "search_customer", "inputSchema": {"type": "object", "properties": {"keyword": {"type": "string"}}}}])
        state = MCPRuntimeState(config=config, client_factory=lambda server: client, reserved_capability_ids=())

        result = await state.refresh(reason="startup", force=True)

        self.assertEqual(result.status, "completed")
        self.assertEqual(state.active_bundle.descriptors, ())
        self.assertEqual(state.active_bundle.diagnostics[0].reason, "invalid_planner_allowlist")

    async def test_prepare_refresh_does_not_activate_bundle_until_commit(self) -> None:
        first_client = FakeClient(tools=[{"name": "search_customer", "inputSchema": {"type": "object", "properties": {"keyword": {"type": "string"}}}}])
        second_client = FakeClient(tools=[{"name": "search_customer", "inputSchema": {"type": "object", "properties": {"keyword": {"type": "string"}}}}])
        clients = iter([first_client, second_client])
        state = MCPRuntimeState(config=self._config(), client_factory=lambda server: next(clients), reserved_capability_ids=())
        await state.refresh(reason="startup", force=True)
        first_revision = state.active_revision

        pending = await state.prepare_refresh(reason="manual", force=True)

        self.assertEqual(pending.result.status, "completed")
        self.assertEqual(state.active_revision, first_revision)
        self.assertNotEqual(pending.bundle.revision, first_revision)
        await state.commit_activation(pending)
        self.assertEqual(state.active_revision, pending.bundle.revision)

    async def test_call_tool_uses_binding_client_and_filtered_arguments(self) -> None:
        client = FakeClient(tools=[{"name": "search_customer", "inputSchema": {"type": "object"}}])
        state = MCPRuntimeState(config=self._config(), client_factory=lambda server: client, reserved_capability_ids=())
        await state.refresh(reason="startup", force=True)

        result = await state.call_tool("mcp.crm.search_customer", {"keyword": "龙粳", "token": "blocked"})

        self.assertEqual(result["structuredContent"], {"ok": True})
        self.assertEqual(client.calls, [("search_customer", {"keyword": "龙粳"})])
