from __future__ import annotations

import unittest

from src.integrations.mcp.client import MCPClient, MCPClientError
from src.integrations.mcp.config import MCPRuntimeConfig
from src.integrations.mcp.protocol import MCP_PROTOCOL_VERSION, MCPStreamEvent, MCPTransportResponse
from src.integrations.mcp.runtime_state import MCPRuntimeState
from src.integrations.mcp.tasks import InMemoryMCPTaskRegistry, validate_related_task_result_metadata


class RecordingTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def send(self, message, *, protocol_version, session_id=None, timeout_seconds=None, last_event_id=None):
        self.requests.append(dict(message))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    async def close(self):
        pass


class TaskFakeClient:
    def __init__(self, *, tools, server_capabilities=None):
        self.tools = tools
        self.server_capabilities = dict(server_capabilities or {})
        self.calls = []
        self.closed = False

    async def list_tools(self):
        return self.tools

    async def call_tool(self, tool_name, arguments, **kwargs):
        self.calls.append((tool_name, dict(arguments), dict(kwargs)))
        if kwargs.get("task_augmented"):
            return {"taskId": "raw-task-1", "status": {"state": "working"}, "pollInterval": 1}
        return {"content": [{"type": "text", "text": "plain"}], "structuredContent": {"plain": True}}

    async def tasks_get(self, task_id):
        return {"taskId": task_id, "status": {"state": "completed", "message": "done"}}

    async def tasks_result(self, task_id):
        return {
            "content": [{"type": "text", "text": "task complete"}],
            "structuredContent": {"ok": True},
            "isError": False,
            "_meta": {"io.modelcontextprotocol/related-task": {"taskId": task_id}},
        }

    async def close(self):
        self.closed = True


class MCPPhase3RuntimeTaskTests(unittest.IsolatedAsyncioTestCase):
    async def test_client_task_augmented_call_and_task_methods_use_standard_shapes(self) -> None:
        transport = RecordingTransport(
            [
                MCPTransportResponse(message={"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {"tasks": {"requests": {"tools.call": {}}}}}}, headers={}),
                MCPTransportResponse(message=None, headers={}),
                MCPTransportResponse(message={"jsonrpc": "2.0", "id": 2, "result": {"taskId": "raw-task-1", "status": {"state": "working"}}}, headers={}),
                MCPTransportResponse(message={"jsonrpc": "2.0", "id": 3, "result": {"taskId": "raw-task-1", "status": {"state": "completed"}}}, headers={}),
                MCPTransportResponse(message={"jsonrpc": "2.0", "id": 4, "result": {"content": [], "_meta": {"io.modelcontextprotocol/related-task": {"taskId": "raw-task-1"}}}}, headers={}),
                MCPTransportResponse(message={"jsonrpc": "2.0", "id": 5, "result": {"cancelled": True}}, headers={}),
            ]
        )
        client = MCPClient(server_id="crm", transport=transport)

        create = await client.call_tool("search_customer", {"keyword": "龙粳"}, task_augmented=True, progress_token="tok-1", task_ttl_ms=60000)
        status = await client.tasks_get("raw-task-1")
        result = await client.tasks_result("raw-task-1")
        cancelled = await client.tasks_cancel("raw-task-1", reason="user_requested")

        self.assertEqual(create["taskId"], "raw-task-1")
        call_params = transport.requests[2]["params"]
        self.assertEqual(call_params["task"], {"ttl": 60000})
        self.assertEqual(call_params["_meta"], {"progressToken": "tok-1"})
        self.assertEqual(transport.requests[3]["method"], "tasks/get")
        self.assertEqual(transport.requests[3]["params"], {"taskId": "raw-task-1"})
        self.assertEqual(transport.requests[4]["method"], "tasks/result")
        self.assertEqual(transport.requests[5]["method"], "tasks/cancel")
        self.assertEqual(status["status"]["state"], "completed")
        self.assertIn("content", result)
        self.assertTrue(cancelled["cancelled"])
        self.assertNotIn("tasks", transport.requests[0]["params"]["capabilities"])

    async def test_runtime_state_negotiates_task_support_and_missing_means_forbidden(self) -> None:
        required_missing = MCPRuntimeConfig.from_mapping(
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
                                "task_augmented_mode": "required",
                            }
                        ],
                    }
                ],
            }
        )
        client = TaskFakeClient(
            tools=[{"name": "search_customer", "inputSchema": {"type": "object", "properties": {"keyword": {"type": "string"}}}}],
            server_capabilities={"tasks": {"requests": {"tools.call": {}}}},
        )
        state = MCPRuntimeState(config=required_missing, client_factory=lambda _server: client)
        await state.refresh(reason="startup", force=True)

        self.assertEqual(state.active_bundle.descriptors, ())
        self.assertEqual(state.active_bundle.diagnostics[0].reason, "task_support_forbidden")

        optional_preferred = MCPRuntimeConfig.from_mapping(
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
                                "task_augmented_mode": "preferred",
                            }
                        ],
                    }
                ],
            }
        )
        client = TaskFakeClient(
            tools=[{"name": "search_customer", "inputSchema": {"type": "object", "properties": {"keyword": {"type": "string"}}}, "execution": {"taskSupport": "optional"}}],
            server_capabilities={"tasks": {"requests": {"tools.call": {}}}},
        )
        state = MCPRuntimeState(config=optional_preferred, client_factory=lambda _server: client)
        await state.refresh(reason="startup", force=True)

        binding = state.binding_for_capability("mcp.crm.search_customer")
        self.assertEqual(binding.task_support, "optional")
        self.assertTrue(binding.task_augmented_call)

    async def test_runtime_state_resolves_create_task_result_through_registry_get_and_result(self) -> None:
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
                                "task_augmented_mode": "required",
                            }
                        ],
                    }
                ],
            }
        )
        client = TaskFakeClient(
            tools=[{"name": "search_customer", "inputSchema": {"type": "object", "properties": {"keyword": {"type": "string"}}}, "execution": {"taskSupport": "required"}}],
            server_capabilities={"tasks": {"requests": {"tools.call": {}}}},
        )
        registry = InMemoryMCPTaskRegistry()
        state = MCPRuntimeState(config=config, client_factory=lambda _server: client, task_registry=registry)
        await state.refresh(reason="startup", force=True)

        result = await state.call_tool("mcp.crm.search_customer", {"keyword": "龙粳"})

        self.assertEqual(result["structuredContent"], {"ok": True})
        self.assertEqual(client.calls[0][2]["task_augmented"], True)
        self.assertEqual(client.calls[0][2]["progress_token"], registry.records()[0].progress_token)
        record = registry.records()[0]
        self.assertEqual(record.status, "completed")
        self.assertRegex(record.safe_ref, r"^mcp-task:crm:search_customer:[A-Fa-f0-9]{32}$")
        self.assertNotIn("raw-task-1", record.safe_ref)

    async def test_runtime_state_emits_progress_from_real_sse_notifications(self) -> None:
        create_task_response = {"jsonrpc": "2.0", "id": 3, "result": {"taskId": "raw-task-1", "status": {"state": "working"}, "pollInterval": 1}}
        transport = RecordingTransport(
            [
                MCPTransportResponse(message={"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {"tasks": {"requests": {"tools.call": {}}}}}}, headers={}),
                MCPTransportResponse(message=None, headers={}),
                MCPTransportResponse(message={"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "search_customer", "inputSchema": {"type": "object", "properties": {"keyword": {"type": "string"}}}, "execution": {"taskSupport": "required"}}]}}, headers={}),
                MCPTransportResponse(
                    message=create_task_response,
                    headers={},
                    sse_events=(
                        MCPStreamEvent(message={"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progressToken": "unused-placeholder", "progress": 5}}),
                        MCPStreamEvent(message=create_task_response),
                    ),
                ),
                MCPTransportResponse(message={"jsonrpc": "2.0", "id": 4, "result": {"taskId": "raw-task-1", "status": {"state": "completed", "message": "done"}}}, headers={}),
                MCPTransportResponse(message={"jsonrpc": "2.0", "id": 5, "result": {"content": [], "structuredContent": {"ok": True}, "_meta": {"io.modelcontextprotocol/related-task": {"taskId": "raw-task-1"}}}}, headers={}),
            ]
        )
        client = MCPClient(server_id="crm", transport=transport)
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
                                "task_augmented_mode": "required",
                            }
                        ],
                    }
                ],
            }
        )
        state = MCPRuntimeState(config=config, client_factory=lambda _server: client)
        await state.refresh(reason="startup", force=True)
        events = []

        async def record(event_type, payload):
            events.append((event_type, dict(payload)))

        async def patched_call_tool(tool_name, arguments, **kwargs):
            token = kwargs["progress_token"]
            transport.responses[0] = MCPTransportResponse(
                message=create_task_response,
                headers={},
                sse_events=(
                    MCPStreamEvent(message={"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progressToken": token, "progress": 5, "total": 10, "message": "warming"}}),
                    MCPStreamEvent(message=create_task_response),
                ),
            )
            return await MCPClient.call_tool(client, tool_name, arguments, **kwargs)

        client.call_tool = patched_call_tool  # type: ignore[method-assign]

        result = await state.call_tool("mcp.crm.search_customer", {"keyword": "龙粳"}, event_callback=record, request_context={"task_id": "platform-task-1"})

        self.assertEqual(result["structuredContent"], {"ok": True})
        self.assertIn("mcp.long_task_progress", [event_type for event_type, _ in events])
        progress_payload = next(payload for event_type, payload in events if event_type == "mcp.long_task_progress")
        self.assertEqual(progress_payload["progress"], 5)
        self.assertEqual(progress_payload["total"], 10)
        self.assertIn("safe_ref", progress_payload)
        self.assertNotIn("raw-task-1", repr(events))

    def test_tasks_result_requires_related_task_metadata(self) -> None:
        validate_related_task_result_metadata({"_meta": {"io.modelcontextprotocol/related-task": {"taskId": "raw-task-1"}}}, "raw-task-1")
        with self.assertRaises(MCPClientError):
            validate_related_task_result_metadata({"_meta": {}}, "raw-task-1")


if __name__ == "__main__":
    unittest.main()
