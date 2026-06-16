from __future__ import annotations

import asyncio
import unittest

from src.integrations.mcp.config import MCPRuntimeConfig
from src.integrations.mcp.runtime_state import MCPRuntimeState
from src.integrations.mcp.tasks import InMemoryMCPTaskRegistry


class CancelFakeClient:
    server_capabilities = {"tasks": {"requests": {"tools.call": {}}}}

    def __init__(self) -> None:
        self.cancelled = []

    async def list_tools(self):
        return [{"name": "search_customer", "inputSchema": {"type": "object"}, "execution": {"taskSupport": "required"}}]

    async def tasks_cancel(self, task_id, *, reason=""):
        self.cancelled.append((task_id, reason))
        return {"cancelled": True}

    async def close(self):
        pass


class InflightCancelFakeClient:
    server_capabilities = {}

    def __init__(self) -> None:
        self.registered = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled_requests = []

    async def list_tools(self):
        return [{"name": "search_customer", "inputSchema": {"type": "object"}}]

    async def call_tool(self, tool_name, arguments, **kwargs):
        callback = kwargs.get("request_registered_callback")
        if callback:
            callback("req-123")
            self.registered.set()
        await self.release.wait()
        return {"content": [{"type": "text", "text": "done"}], "structuredContent": {"ok": True}, "isError": False}

    async def cancel_request(self, request_id, *, reason=""):
        self.cancelled_requests.append((request_id, reason))

    async def close(self):
        pass


class MCPPhase4CancelBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_cancel_platform_task_uses_tasks_cancel_and_returns_sanitized_events(self) -> None:
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
                                "task_augmented_mode": "required",
                            }
                        ],
                    }
                ],
            }
        )
        client = CancelFakeClient()
        registry = InMemoryMCPTaskRegistry()
        state = MCPRuntimeState(config=config, client_factory=lambda _server: client, task_registry=registry)
        await state.refresh(reason="startup", force=True)
        token = registry.make_progress_token(server_id="crm", tool_name="search_customer")
        registry.create_record(
            server_id="crm",
            tool_name="search_customer",
            capability_id="mcp.crm.search_customer",
            mcp_task_id="raw-task-1",
            progress_token=token,
            status_payload={"status": {"state": "working"}},
            platform_task_id="task-1",
            platform_node_id="lookup",
            conversation_id="conv-1",
        )

        events = await state.cancel_platform_task("task-1", reason="user_requested")

        self.assertEqual(client.cancelled, [("raw-task-1", "user_requested")])
        self.assertEqual([event["event_type"] for event in events], ["mcp.long_task_cancel_requested", "mcp.long_task_cancelled"])
        serialized = repr(events)
        self.assertIn("safe_ref", serialized)
        self.assertNotIn("raw-task-1", serialized)

    async def test_runtime_cancel_platform_task_uses_cancelled_notification_for_inflight_plain_request(self) -> None:
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
                                "planner_allowed_fields": ["keyword"],
                            }
                        ],
                    }
                ],
            }
        )
        client = InflightCancelFakeClient()
        state = MCPRuntimeState(config=config, client_factory=lambda _server: client)
        await state.refresh(reason="startup", force=True)

        call = asyncio.create_task(
            state.call_tool(
                "mcp.crm.search_customer",
                {"keyword": "龙粳"},
                request_context={"task_id": "platform-task-1", "node_id": "lookup", "conversation_id": "conv-1"},
            )
        )
        await asyncio.wait_for(client.registered.wait(), timeout=1)

        events = await state.cancel_platform_task("platform-task-1", reason="user_requested")
        client.release.set()
        await call

        self.assertEqual(client.cancelled_requests, [("req-123", "user_requested")])
        self.assertEqual([event["event_type"] for event in events], ["mcp.long_task_cancel_requested", "mcp.long_task_cancelled"])
        serialized = repr(events)
        self.assertIn("safe_ref", serialized)
        self.assertNotIn("req-123", serialized)


if __name__ == "__main__":
    unittest.main()
