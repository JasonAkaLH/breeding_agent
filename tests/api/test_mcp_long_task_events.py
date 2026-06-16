from __future__ import annotations

import json

from tests.api.support import APITestCase


MCP_LONG_TASK_CONFIG = {
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
                    "task_augmented_mode": "required",
                }
            ],
        }
    ],
}


class LongTaskMCPClient:
    server_capabilities = {"tasks": {"requests": {"tools.call": {}}}}

    def __init__(self) -> None:
        self.calls = []

    async def list_tools(self):
        return [
            {
                "name": "search_customer",
                "inputSchema": {"type": "object", "properties": {"keyword": {"type": "string"}}},
                "execution": {"taskSupport": "required"},
            }
        ]

    async def call_tool(self, tool_name, arguments, **kwargs):
        self.calls.append((tool_name, dict(arguments), dict(kwargs)))
        return {"taskId": "raw-task-api-1", "status": {"state": "working", "message": "queued"}, "pollInterval": 1}

    async def tasks_get(self, task_id):
        return {"taskId": task_id, "status": {"state": "completed", "message": "done"}}

    async def tasks_result(self, task_id):
        return {
            "content": [{"type": "text", "text": "客户基础信息：龙粳33"}],
            "structuredContent": {"name": "龙粳33"},
            "isError": False,
            "_meta": {"io.modelcontextprotocol/related-task": {"taskId": task_id}},
        }

    async def close(self):
        pass


class MCPLongTaskEventsAPITests(APITestCase):
    async def test_mcp_long_task_events_are_persisted_before_terminal_task_event_and_redacted(self) -> None:
        mcp_client = LongTaskMCPClient()

        def planner(_prompt, **_kwargs):
            return json.dumps(
                {
                    "nodes": [
                        {
                            "node_id": "lookup",
                            "capability_id": "mcp.crm.search_customer",
                            "input_payload": {"keyword": "龙粳", "token": "SECRET"},
                        }
                    ]
                },
                ensure_ascii=False,
            )

        await self.reconfigure_runtime(
            mcp_config=MCP_LONG_TASK_CONFIG,
            mcp_client_factory=lambda _server: mcp_client,
            planner_text_generator=planner,
            main_agent_stream_generator=lambda _prompt, **_kwargs: "完成。",
        )
        response = await self.submit_message(content="查一下龙粳的客户信息", capability_id=None)
        response.raise_for_status()
        task_id = response.json()["task_id"]

        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")

        events = await self.runtime.storage.list_events_for_task(task_id)
        event_types = [event.event_type for event in events]
        self.assertLess(event_types.index("mcp.long_task_started"), event_types.index("task.completed"))
        self.assertLess(event_types.index("mcp.long_task_completed"), event_types.index("task.completed"))
        payloads = [event.payload for event in events if event.event_type.startswith("mcp.long_task_")]
        serialized = repr(payloads)
        self.assertIn("safe_ref", serialized)
        self.assertNotIn("raw-task-api-1", serialized)
        self.assertNotIn("SECRET", serialized)
        self.assertTrue(mcp_client.calls[0][2]["task_augmented"])


if __name__ == "__main__":
    import unittest

    unittest.main()
