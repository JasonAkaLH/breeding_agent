from __future__ import annotations

import unittest

from src.integrations.mcp.config import MCPRuntimeConfig
from src.integrations.mcp.runtime_state import MCPRuntimeState
from src.orchestration.registry import CapabilityRegistry


class FakeClient:
    async def list_tools(self):
        return [
            {"name": "search_customer", "description": "server-side hidden", "inputSchema": {"type": "object", "properties": {"keyword": {"type": "string"}}}},
            {"name": "hidden_tool", "description": "do not expose"},
        ]

    async def close(self):
        pass


class MCPCapabilityRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_public_mcp_descriptor_enters_registry_with_model_allowlist(self) -> None:
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
                                "public_description": "查询客户基础信息。",
                                "risk_level": "read_only",
                                "model_allowed_fields": ["keyword"],
                            },
                            {"tool_name": "hidden_tool", "expose": False},
                        ],
                    }
                ],
            }
        )
        state = MCPRuntimeState(config=config, client_factory=lambda server: FakeClient(), reserved_capability_ids=())
        await state.refresh(reason="startup", force=True)
        registry = CapabilityRegistry()
        for descriptor in state.active_bundle.descriptors:
            registry.register(descriptor)

        public_ids = [descriptor.capability_id for descriptor in registry.list(public_only=True)]
        self.assertEqual(public_ids, ["mcp.crm.search_customer"])
        binding = state.active_bundle.bindings["mcp.crm.search_customer"]
        self.assertEqual(binding.model_allowed_fields, ("keyword",))
