from __future__ import annotations

import unittest

from src.integrations.mcp.config import MCPRuntimeConfig
from src.integrations.mcp.runtime_state import MCPRuntimeState
from src.orchestration.models import OrchestrationRequest, WorkflowNodePlan
from src.orchestration.planner_payload_policy import PlannerPayloadPolicy
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
    async def test_only_public_mcp_descriptor_enters_registry_and_payload_policy_fails_closed(self) -> None:
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
                                "planner_allowed_fields": ["keyword"],
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
            registry.register(descriptor, planner_payload_policy=state.active_bundle.payload_policies[descriptor.capability_id])

        public_ids = [descriptor.capability_id for descriptor in registry.list(public_only=True)]
        self.assertEqual(public_ids, ["mcp.crm.search_customer"])
        self.assertIn("mcp.crm.search_customer", registry.planner_payload_policies())

        node = WorkflowNodePlan(
            node_id="lookup",
            capability_id="mcp.crm.search_customer",
            input_payload={"keyword": "龙粳", "endpoint": "https://evil", "token": "SECRET"},
        )
        filtered = PlannerPayloadPolicy(registry.planner_payload_policies()).apply(
            node,
            request=OrchestrationRequest(task_id="task-1", conversation_id="conv-1", root_message_id="msg-1", user_message="查客户"),
        )
        self.assertEqual(filtered.input_payload, {"keyword": "龙粳"})
