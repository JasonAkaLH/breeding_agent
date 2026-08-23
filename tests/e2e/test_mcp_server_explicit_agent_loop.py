from __future__ import annotations

from datetime import datetime
import os
from unittest.mock import patch

from src.capabilities.mcp_dispatch import (
    MCPDispatchExecutor,
    MCPSelectorAction,
    MCPSelectorActionType,
)
from src.core.enums import UserMCPHealthStatus, UserMCPTransport
from src.core.models import UserMCPServer
from src.integrations.mcp.cp7_artifacts import (
    canonical_json_bytes,
    mcp_no_server_intent_id,
)
from src.integrations.mcp.resume_envelope import (
    MCP_DISPATCH_RESUME_ENVELOPE_SCHEMA_V2,
)
from src.integrations.mcp.gateway_models import MCPTaskServerScope, ToolCatalogSnapshot
from tests.api.support import APITestCase


class _FinishSelector:
    def __init__(self) -> None:
        self.contexts = []

    async def select(self, context):
        self.contexts.append(context)
        return MCPSelectorAction(MCPSelectorActionType.FINISH, reason="discovery is sufficient")


class _FixedGateway:
    def __init__(self) -> None:
        self.opened = []
        self.listed = []
        self.closed = []
        self.calls = []

    async def open_scope(self, principal, task_id, server_id, **_callbacks):
        self.opened.append((principal.username, task_id, server_id))
        return MCPTaskServerScope("scope-fixed", principal.username, task_id, server_id, 1, 1)

    async def list_tools(self, scope):
        self.listed.append(scope.server_id)
        return ToolCatalogSnapshot(
            server_id=scope.server_id,
            effective_protocol_version="2025-11-25",
            tools=(),
        )

    async def call_tool(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        raise AssertionError("finish must not call an MCP tool")

    async def close_scope(self, scope, reason):
        self.closed.append((scope.scope_id, reason))


class _ForbiddenRouter:
    async def route(self, **kwargs):
        raise AssertionError(f"explicit binding must not call Server Router: {kwargs}")


class MCPServerExplicitAgentLoopE2ETest(APITestCase):
    def build_runtime(self, **kwargs):
        kwargs.setdefault(
            "main_agent_stream_generator",
            lambda _prompt, **_options: '{"action":"finish","reason":"done"}',
        )
        with patch.dict(
            os.environ,
            {
                "MAF_USER_MCP_MAX_ACTIVE_CALLS": "2",
                "MAF_USER_MCP_TEMPORARY_DISK_LOW_WATERMARK_BYTES": "1",
            },
            clear=False,
        ):
            return super().build_runtime(
                **kwargs,
                enable_user_mcp=True,
                enable_user_mcp_routing=True,
            )

    async def test_fixed_server_discovery_finish_agent_final_and_history(self) -> None:
        now = datetime(2026, 8, 17, 10, 0, 0)
        await self.runtime.storage.create_user_mcp_server(
            UserMCPServer(
                server_id="mcp-ocr",
                owner_user_id="acc-1",
                display_name="OCR服务",
                routing_description="识别文件",
                endpoint_url="https://mcp.example.test/rpc",
                transport=UserMCPTransport.STREAMABLE_HTTP,
                health_status=UserMCPHealthStatus.AVAILABLE,
                created_at=now,
                updated_at=now,
            )
        )
        executor = next(
            item
            for item in (
                self.runtime._agent_capability_invoker
                ._invocation._executor._executors
            )
            if isinstance(item, MCPDispatchExecutor)
        )
        coordinator = executor._coordinator
        gateway = _FixedGateway()
        selector = _FinishSelector()
        coordinator._gateway = gateway
        coordinator._selector = selector
        coordinator._server_router = _ForbiddenRouter()

        response = await self.client.post(
            "/api/v1/conversations/chat-messages",
            json={
                "conversation_id": "conv-mcp-e2e",
                "content": "列出可用工具并说明能力",
                "routing_mode": "force_capability",
                "capability_id": "mcp.dispatch",
                "metadata": {
                    "mcp_server_binding": {"server_id": "mcp-ocr"},
                    "deep_thinking": False,
                },
            },
        )

        self.assertEqual(response.status_code, 202, response.text)
        terminal = await self.wait_for_terminal_task(response.json()["task_id"])
        events = await self.runtime.storage.list_events_for_task(
            response.json()["task_id"]
        )
        self.assertEqual(
            terminal["status"],
            "completed",
            [(event.event_type, event.payload) for event in events],
        )
        nodes = await self.runtime.storage.list_task_nodes_for_task(response.json()["task_id"])
        self.assertEqual(
            {node.capability_id for node in nodes},
            {"mcp.dispatch", "agent.final_output"},
        )
        dispatch_node = next(
            node for node in nodes if node.capability_id == "mcp.dispatch"
        )
        intent = await self.runtime.storage.get_mcp_no_server_intent(
            mcp_no_server_intent_id(
                response.json()["task_id"], node_id=dispatch_node.node_id
            )
        )
        self.assertIsNotNone(intent)
        envelope = dict(intent.resume_envelope_json)
        self.assertEqual(
            envelope["schema"], MCP_DISPATCH_RESUME_ENVELOPE_SCHEMA_V2
        )
        self.assertNotIn("metadata", envelope)
        self.assertNotIn("input_payload", envelope)
        self.assertNotIn("dependency_outputs", envelope)
        self.assertLess(len(canonical_json_bytes(envelope)), 4 * 1024)
        self.assertEqual(len(gateway.opened), 1)
        self.assertEqual(gateway.opened[0][2], "mcp-ocr")
        self.assertEqual(gateway.listed, ["mcp-ocr"])
        self.assertEqual(gateway.calls, [])
        self.assertEqual(len(gateway.closed), 1)
        self.assertEqual(len(selector.contexts), 1)
        self.assertEqual(selector.contexts[0].binding_mode.value, "explicit_command")
        self.assertFalse(selector.contexts[0].allow_route_another_server)

        history = await self.client.get("/api/v1/conversations/conv-mcp-e2e/messages")
        self.assertEqual(history.status_code, 200, history.text)
        user_message = next(item for item in history.json()["messages"] if item["role"] == "user")
        self.assertEqual(user_message["metadata"]["mcp_server_badge"]["command"], "$OCR服务")
        self.assertNotIn("mcp_server_binding_context", user_message["metadata"])


if __name__ == "__main__":
    import unittest

    unittest.main()
