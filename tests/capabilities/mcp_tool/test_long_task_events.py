from __future__ import annotations

import asyncio
import unittest

from src.capabilities.mcp_tool.executor import MCPToolExecutor
from src.core.contracts import CapabilityExecutionRequest
from src.core.enums import EventVisibility
from src.integrations.mcp.config import MCPRuntimeConfig
from src.integrations.mcp.runtime_state import MCPRuntimeState, MCPToolBinding


class LongTaskRuntime:
    def __init__(self) -> None:
        self.binding = MCPToolBinding(
            capability_id="mcp.crm.search_customer",
            server_id="crm",
            tool_name="search_customer",
            planner_allowed_fields=("keyword",),
            input_schema={"type": "object", "properties": {"keyword": {"type": "string"}}},
            task_augmented_call=True,
        )

    def active_mcp_capability_ids(self):
        return (self.binding.capability_id,)

    def binding_for_capability(self, capability_id: str, revision=None):
        return self.binding

    async def call_tool(self, capability_id: str, arguments, revision=None, event_callback=None, request_context=None):
        assert request_context["task_id"] == "task-1"
        await event_callback(
            "mcp.long_task_started",
            {"server_id": "crm", "tool_name": "search_customer", "capability_id": capability_id, "safe_ref": "mcp-task:crm:search_customer:00000000000000000000000000000001"},
        )
        await event_callback(
            "mcp.long_task_progress",
            {"server_id": "crm", "tool_name": "search_customer", "progress": 50, "total": 100, "message": "half", "safe_ref": "mcp-task:crm:search_customer:00000000000000000000000000000001"},
        )
        await event_callback(
            "mcp.long_task_completed",
            {"safe_ref": "mcp-task:crm:search_customer:00000000000000000000000000000001", "duration_ms": 5, "output_size_bytes": 12, "truncated": False},
        )
        return {"content": [{"type": "text", "text": "done"}], "structuredContent": {"ok": True}, "isError": False}


class CancelledRuntime(LongTaskRuntime):
    def __init__(self) -> None:
        super().__init__()
        self.cancelled = []

    async def call_tool(self, capability_id: str, arguments, revision=None, event_callback=None, request_context=None):
        raise asyncio.CancelledError

    async def cancel_platform_task(self, task_id: str, *, reason: str = ""):
        self.cancelled.append((task_id, reason))
        return []


class InflightMCPClient:
    server_capabilities = {}

    def __init__(self) -> None:
        self.registered = asyncio.Event()
        self.cancelled_requests = []

    async def list_tools(self):
        return [{"name": "search_customer", "inputSchema": {"type": "object", "properties": {"keyword": {"type": "string"}}}}]

    async def call_tool(self, tool_name, arguments, **kwargs):
        callback = kwargs.get("request_registered_callback")
        if callback:
            callback("req-123")
            self.registered.set()
        await asyncio.sleep(30)
        return {"content": [{"type": "text", "text": "done"}], "structuredContent": {"ok": True}, "isError": False}

    async def cancel_request(self, request_id, *, reason: str = ""):
        self.cancelled_requests.append((request_id, reason))

    async def close(self):
        pass


class MCPToolLongTaskEventTests(unittest.IsolatedAsyncioTestCase):
    async def test_executor_records_sanitized_frontend_long_task_events_live(self) -> None:
        recorded = []

        async def recorder(event):
            recorded.append(event)

        executor = MCPToolExecutor(runtime_state=LongTaskRuntime(), live_event_recorder=recorder)
        request = CapabilityExecutionRequest(
            capability_id="mcp.crm.search_customer",
            conversation_id="conv-1",
            task_id="task-1",
            node_id="lookup",
            input_payload={"keyword": "龙粳"},
            metadata={"mcp_execution_mode": "legacy"},
        )

        result = await executor.execute(request)

        self.assertIsNone(result.error)
        self.assertEqual([event.event_type for event in recorded], ["mcp.long_task_started", "mcp.long_task_progress", "mcp.long_task_completed"])
        self.assertTrue(all(event.visibility == EventVisibility.FRONTEND for event in recorded))
        serialized = repr([event.payload for event in recorded])
        self.assertIn("safe_ref", serialized)
        self.assertNotIn("raw-task", serialized)
        self.assertNotIn("progressToken", serialized)

    async def test_executor_propagates_platform_cancellation_to_mcp_runtime_before_reraising(self) -> None:
        runtime = CancelledRuntime()
        executor = MCPToolExecutor(runtime_state=runtime)
        request = CapabilityExecutionRequest(
            capability_id="mcp.crm.search_customer",
            conversation_id="conv-1",
            task_id="task-1",
            node_id="lookup",
            input_payload={"keyword": "龙粳"},
            metadata={"mcp_execution_mode": "legacy"},
        )

        with self.assertRaises(asyncio.CancelledError):
            await executor.execute(request)

        self.assertEqual(runtime.cancelled, [("task-1", "platform_cancelled")])

    async def test_executor_cancellation_sends_mcp_cancelled_notification_for_real_inflight_request(self) -> None:
        client = InflightMCPClient()
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
        runtime = MCPRuntimeState(config=config, client_factory=lambda _server: client)
        await runtime.refresh(reason="startup", force=True)
        executor = MCPToolExecutor(runtime_state=runtime)
        request = CapabilityExecutionRequest(
            capability_id="mcp.crm.search_customer",
            conversation_id="conv-1",
            task_id="task-1",
            node_id="lookup",
            input_payload={"keyword": "龙粳"},
            metadata={"mcp_execution_mode": "legacy"},
        )

        execution = asyncio.create_task(executor.execute(request))
        await asyncio.wait_for(client.registered.wait(), timeout=1)
        execution.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await execution

        self.assertEqual(client.cancelled_requests, [("req-123", "platform_cancelled")])


if __name__ == "__main__":
    unittest.main()
