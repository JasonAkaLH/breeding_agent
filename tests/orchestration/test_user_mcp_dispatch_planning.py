from __future__ import annotations

import json
import unittest

from src.capabilities.main_agent import (
    MAIN_AGENT_CAPABILITY_DESCRIPTORS,
    MAIN_AGENT_PLANNER_PAYLOAD_POLICIES,
    MainAgentWorkflowProvider,
)
from src.capabilities.mcp_dispatch import (
    MCP_DISPATCH_CAPABILITY_DESCRIPTOR,
    MCP_DISPATCH_PLANNER_PAYLOAD_POLICY,
)
from src.orchestration.llm_workflow_provider import LLMWorkflowProvider
from src.orchestration.models import CapabilityDescriptor, OrchestrationRequest, UserMCPServerProfile
from src.orchestration.registry import CapabilityRegistry


class UserMCPDispatchPlanningTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.registry = CapabilityRegistry()
        for descriptor in MAIN_AGENT_CAPABILITY_DESCRIPTORS:
            self.registry.register(
                descriptor,
                planner_payload_policy=MAIN_AGENT_PLANNER_PAYLOAD_POLICIES[descriptor.capability_id],
            )
        self.registry.register(
            MCP_DISPATCH_CAPABILITY_DESCRIPTOR,
            planner_payload_policy=MCP_DISPATCH_PLANNER_PAYLOAD_POLICY,
        )

    def make_provider(self, planner) -> LLMWorkflowProvider:
        return LLMWorkflowProvider(
            capability_registry=self.registry,
            fallback_provider=MainAgentWorkflowProvider(),
            macro_providers={},
            text_generator=planner,
        )

    async def test_safe_server_profile_exposes_dispatch_and_finalizer_depends_on_it(self) -> None:
        prompts: list[str] = []

        def planner(prompt: str) -> str:
            prompts.append(prompt)
            return json.dumps(
                {
                    "nodes": [
                        {
                            "node_id": "route_mcp",
                            "capability_id": "mcp.dispatch",
                            "input_payload": {
                                "server_id": "server-safe",
                                "endpoint": "https://must-not-survive.example",
                                "tool_name": "must-not-survive",
                            },
                        }
                    ]
                }
            )

        request = OrchestrationRequest(
            task_id="task-mcp",
            conversation_id="conv-1",
            root_message_id="msg-1",
            user_message="查询我的 CRM 客户",
            available_mcp_servers=(
                UserMCPServerProfile(
                    server_id="server-safe",
                    display_name="我的 CRM",
                    routing_description="查询客户和订单",
                    transport="streamable_http",
                ),
            ),
        )
        plan = await self.make_provider(planner).build_plan(request)

        self.assertIn("mcp.dispatch", prompts[0])
        self.assertIn('"server_id":"server-safe"', prompts[0])
        self.assertIn('"display_name":"我的 CRM"', prompts[0])
        self.assertIn('"routing_description":"查询客户和订单"', prompts[0])
        self.assertIn('"transport":"streamable_http"', prompts[0])
        self.assertNotIn("must-not-survive.example", prompts[0])
        self.assertNotIn("must-not-survive", prompts[0])
        self.assertEqual([node.capability_id for node in plan.nodes], ["mcp.dispatch", "main_agent.respond"])
        self.assertEqual(plan.nodes[0].input_payload, {"server_id": "server-safe"})
        self.assertEqual(plan.nodes[1].depends_on, (plan.nodes[0].node_id,))
        self.assertTrue(plan.metadata["planner_finalizer_added"])

    async def test_dispatch_is_hidden_and_rejected_without_profiles(self) -> None:
        prompts: list[str] = []

        def planner(prompt: str) -> str:
            prompts.append(prompt)
            if len(prompts) == 1:
                return '{"nodes":[{"node_id":"bad","capability_id":"mcp.dispatch","input_payload":{"server_id":"x"}}]}'
            return '{"nodes":[{"node_id":"answer","capability_id":"main_agent.respond"}]}'

        request = OrchestrationRequest(
            task_id="task-no-profiles",
            conversation_id="conv-1",
            root_message_id="msg-1",
            user_message="查一下外部系统",
        )
        plan = await self.make_provider(planner).build_plan(request)

        self.assertNotIn("mcp.dispatch", prompts[0])
        self.assertEqual([node.capability_id for node in plan.nodes], ["main_agent.respond"])
        self.assertEqual(plan.metadata["planner_repair_attempts"], 1)

    async def test_existing_main_agent_tail_is_rewired_to_dispatch(self) -> None:
        def planner(_prompt: str) -> str:
            return json.dumps(
                {
                    "nodes": [
                        {
                            "node_id": "route_mcp",
                            "capability_id": "mcp.dispatch",
                            "input_payload": {"server_id": "server-safe"},
                        },
                        {"node_id": "answer", "capability_id": "main_agent.respond"},
                    ]
                }
            )

        plan = await self.make_provider(planner).build_plan(
            OrchestrationRequest(
                task_id="task-rewire",
                conversation_id="conv-1",
                root_message_id="msg-1",
                user_message="查询 CRM 后回答",
                available_mcp_servers=(
                    UserMCPServerProfile("server-safe", "CRM", "查询客户", "streamable_http"),
                ),
            )
        )

        self.assertEqual([node.capability_id for node in plan.nodes], ["mcp.dispatch", "main_agent.respond"])
        self.assertEqual(plan.nodes[1].depends_on, (plan.nodes[0].node_id,))
        self.assertFalse(plan.metadata["planner_finalizer_added"])
        self.assertTrue(plan.metadata["planner_finalizer_rewired"])

    def test_registry_has_one_generic_descriptor_and_filters_it_per_request(self) -> None:
        no_profiles = OrchestrationRequest("task-1", "conv-1", "msg-1", "hello")
        with_profiles = OrchestrationRequest(
            "task-2",
            "conv-1",
            "msg-2",
            "hello",
            available_mcp_servers=(
                UserMCPServerProfile("server-1", "CRM", "查询 CRM", "streamable_http"),
            ),
        )

        self.assertNotIn(
            "mcp.dispatch",
            {item.capability_id for item in self.registry.list_for_request(no_profiles, public_only=True)},
        )
        self.assertEqual(
            [
                item.capability_id
                for item in self.registry.list_for_request(with_profiles, public_only=True)
                if item.capability_id == "mcp.dispatch"
            ],
            ["mcp.dispatch"],
        )
        self.assertEqual(MCP_DISPATCH_PLANNER_PAYLOAD_POLICY.planner_allowed_fields, ("server_id",))

    def test_registry_exposes_exactly_the_task_assigned_mcp_path(self) -> None:
        self.registry.register(
            CapabilityDescriptor(
                capability_id="mcp.crm.search_customer",
                name="search_customer",
                description="legacy MCP tool",
                kind="mcp_tool",
                source="mcp",
            )
        )
        profiles = (UserMCPServerProfile("server-1", "CRM", "查询 CRM", "streamable_http"),)

        user_scoped = self.registry.list_for_request(
            OrchestrationRequest(
                "task-user",
                "conv-1",
                "msg-user",
                "hello",
                metadata={"mcp_execution_mode": "user_scoped"},
                available_mcp_servers=profiles,
            ),
            public_only=True,
        )
        legacy = self.registry.list_for_request(
            OrchestrationRequest(
                "task-legacy",
                "conv-1",
                "msg-legacy",
                "hello",
                metadata={"mcp_execution_mode": "legacy"},
                available_mcp_servers=profiles,
            ),
            public_only=True,
        )
        unavailable = self.registry.list_for_request(
            OrchestrationRequest(
                "task-none",
                "conv-1",
                "msg-none",
                "hello",
                metadata={"mcp_execution_mode": "unavailable"},
                available_mcp_servers=profiles,
            ),
            public_only=True,
        )

        self.assertIn("mcp.dispatch", {item.capability_id for item in user_scoped})
        self.assertNotIn("mcp.crm.search_customer", {item.capability_id for item in user_scoped})
        self.assertNotIn("mcp.dispatch", {item.capability_id for item in legacy})
        self.assertIn("mcp.crm.search_customer", {item.capability_id for item in legacy})
        self.assertNotIn("mcp.dispatch", {item.capability_id for item in unavailable})
        self.assertNotIn("mcp.crm.search_customer", {item.capability_id for item in unavailable})


if __name__ == "__main__":
    unittest.main()
