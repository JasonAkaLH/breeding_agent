from __future__ import annotations

from datetime import datetime
import hashlib
import json
import os
from types import SimpleNamespace
from unittest.mock import patch

from src.capabilities.mcp_dispatch import (
    MCPDispatchExecutor,
    MCPSelectorAction,
    MCPSelectorActionType,
)
from src.core.enums import UserMCPHealthStatus, UserMCPTransport
from src.core.models import UserMCPServer, UserMCPToolGrant
from src.integrations.mcp.cp7_artifacts import (
    canonical_json_bytes,
    mcp_durable_result_artifact_id,
    mcp_no_server_intent_id,
)
from src.integrations.mcp.endpoint_policy import EndpointPolicy
from src.integrations.mcp.resume_envelope import (
    MCP_DISPATCH_RESUME_ENVELOPE_SCHEMA_V2,
)
from src.integrations.mcp.gateway_models import (
    MCPTaskServerScope,
    ToolCatalogSnapshot,
)
from src.storage.artifact_files import parse_file_storage_ref
from tests.api.support import APITestCase


class _FinishSelector:
    def __init__(self, reason: str = "discovery is sufficient") -> None:
        self.contexts = []
        self.reason = reason

    async def select(self, context):
        self.contexts.append(context)
        return MCPSelectorAction(MCPSelectorActionType.FINISH, reason=self.reason)


class _CallThenFinishSelector:
    def __init__(self, sentinel: str) -> None:
        self.sentinel = sentinel
        self.contexts = []

    async def select(self, context):
        self.contexts.append(context)
        if not context.completed_result_projections:
            return MCPSelectorAction(
                MCPSelectorActionType.CALL_TOOL,
                tool_name="lookup",
                arguments={"query": "rice"},
            )
        if self.sentinel not in context.completed_result_projections[-1]:
            raise AssertionError("Selector did not receive the parsed Tool result")
        return MCPSelectorAction(MCPSelectorActionType.FINISH)


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


class _ResultAdapter:
    def __init__(self, result_text: str) -> None:
        self.result_text = result_text
        self.server_capabilities = {"tools": {}}
        self.negotiated_session = SimpleNamespace(
            negotiated_protocol_version="2025-11-25"
        )
        self.call_count = 0
        self.closed = False

    async def initialize(self):
        return None

    async def list_tools(self):
        return [
            {
                "name": "lookup",
                "description": "Return project statistics",
                "inputSchema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                    "additionalProperties": False,
                },
            }
        ]

    async def call_tool(self, tool_name, arguments, **kwargs):
        self.call_count += 1
        self.last_call = (tool_name, dict(arguments))
        callback = kwargs.get("request_registered_callback")
        if callback is not None:
            callback("remote-request-1")
        sink = kwargs["result_sink"]
        await sink.write(
            json.dumps(
                {
                    "content": [
                        {"type": "text", "text": self.result_text}
                    ],
                    "ignoredInternalValue": "do-not-leak-to-agent",
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        result_ref = await sink.finalize()
        return {"_mcpResultRef": result_ref.as_payload()}

    async def close(self):
        self.closed = True


class _PublicEndpointResolver:
    def resolve(self, hostname, port):
        del hostname, port
        return ("8.8.8.8",)


class _ForbiddenRouter:
    async def route(self, **kwargs):
        raise AssertionError(f"explicit binding must not call Server Router: {kwargs}")


class MCPServerExplicitAgentLoopE2ETest(APITestCase):
    def build_runtime(self, **kwargs):
        kwargs.setdefault(
            "main_agent_stream_generator",
            lambda _prompt, **_options: '{"action":"finish","reason":"done"}',
        )
        if kwargs.get("enable_user_mcp") is None:
            kwargs["enable_user_mcp"] = True
        if kwargs.get("enable_user_mcp_routing") is None:
            kwargs["enable_user_mcp_routing"] = True
        with patch.dict(
            os.environ,
            {
                "MAF_USER_MCP_MAX_ACTIVE_CALLS": "2",
                "MAF_USER_MCP_TEMPORARY_DISK_LOW_WATERMARK_BYTES": "1",
            },
            clear=False,
        ):
            return super().build_runtime(**kwargs)

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

    async def test_actual_tool_result_reaches_next_agent_model_request_once(self) -> None:
        sentinel = "MCP_RESULT_START_SENTINEL"
        end_sentinel = "MCP_RESULT_END_SENTINEL"
        prompts: list[str] = []

        def agent_fixture(prompt: str, **_kwargs):
            prompts.append(prompt)
            if sentinel not in prompt or end_sentinel not in prompt:
                raise AssertionError("main Agent did not receive the MCP Tool result")
            return "已根据MCP结果完成。"

        await self.reconfigure_runtime(main_agent_stream_generator=agent_fixture)
        now = datetime(2026, 8, 29, 10, 0, 0)
        await self.runtime.storage.create_user_mcp_server(
            UserMCPServer(
                server_id="mcp-summary",
                owner_user_id="acc-1",
                display_name="Summary服务",
                routing_description="查询摘要",
                endpoint_url="https://mcp.example.test/rpc",
                transport=UserMCPTransport.STREAMABLE_HTTP,
                health_status=UserMCPHealthStatus.AVAILABLE,
                created_at=now,
                updated_at=now,
            )
        )
        executor = next(
            item
            for item in self.runtime._agent_capability_invoker._invocation._executor._executors
            if isinstance(item, MCPDispatchExecutor)
        )
        coordinator = executor._coordinator
        gateway = coordinator._gateway
        adapter = _ResultAdapter(f"{sentinel} project_count=7 {end_sentinel}")
        endpoint_policy = EndpointPolicy(resolver=_PublicEndpointResolver())
        gateway._client_factory = lambda server, credentials, endpoint: adapter
        gateway._credential_loader = lambda server: {}
        gateway._endpoint_revalidator = lambda server: endpoint_policy.validate(
            server.endpoint_url
        )
        selector = _CallThenFinishSelector(sentinel)
        coordinator._selector = selector
        router = _ForbiddenRouter()
        coordinator._server_router = router
        input_schema = {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        }
        input_schema_sha256 = hashlib.sha256(
            json.dumps(
                input_schema,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        await self.runtime.storage.save_user_mcp_tool_grant(
            UserMCPToolGrant(
                grant_id="grant-mcp-result-e2e",
                owner_user_id="acc-1",
                server_id="mcp-summary",
                tool_name="lookup",
                server_security_version=1,
                input_schema_sha256=input_schema_sha256,
                granted_at=now,
            )
        )

        response = await self.client.post(
            "/api/v1/conversations/chat-messages",
            json={
                "conversation_id": "conv-mcp-result-text",
                "content": "查询rice并给出结果",
                "routing_mode": "force_capability",
                "capability_id": "mcp.dispatch",
                "metadata": {
                    "mcp_server_binding": {"server_id": "mcp-summary"},
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
        self.assertEqual(adapter.call_count, 1)
        self.assertEqual(adapter.last_call, ("lookup", {"query": "rice"}))
        self.assertTrue(adapter.closed)
        self.assertEqual(len(selector.contexts), 2)
        self.assertFalse(selector.contexts[0].allow_route_another_server)
        self.assertFalse(hasattr(router, "kwargs"))
        calls = await self.runtime.storage.list_mcp_call_records(
            "acc-1", response.json()["task_id"]
        )
        self.assertEqual(len(calls), 1)
        receipt = await self.runtime.storage.get_mcp_terminal_result_receipt_for_call(
            calls[0].call_ref
        )
        self.assertEqual(receipt.result_parser_revision, "mcp-result-parser.v2")
        artifact = await self.runtime.storage.get_artifact(
            mcp_durable_result_artifact_id(calls[0].result_ref)
        )
        metadata = parse_file_storage_ref(artifact.storage_ref)
        self.assertEqual(
            metadata["projection_schema"],
            "maf.mcp.parsed_result_projection.v2",
        )
        matching_prompts = [prompt for prompt in prompts if sentinel in prompt]
        self.assertEqual(len(matching_prompts), 1)
        prompt = matching_prompts[0]
        self.assertEqual(prompt.count(sentinel), 1)
        self.assertEqual(prompt.count(end_sentinel), 1)
        self.assertIn("maf.mcp.agent_result_bundle.v1", prompt)
        self.assertIn('"result_count":1', prompt)
        self.assertNotIn("results are available by safe reference", prompt)
        self.assertNotIn("do-not-leak-to-agent", prompt)
        self.assertNotIn(calls[0].result_ref, prompt)

    async def test_oversized_actual_tool_result_exposes_source_and_carrier_truncation(self) -> None:
        sentinel = "MCP_OVERSIZED_START"
        end_sentinel = "MCP_OVERSIZED_END"
        prompts: list[str] = []

        def agent_fixture(prompt: str, **_kwargs):
            prompts.append(prompt)
            required = (
                sentinel,
                '"source_truncated":true',
                '"carrier_truncated":true',
                '"projection_truncated":true',
            )
            if any(value not in prompt for value in required):
                raise AssertionError("main Agent did not receive truncation evidence")
            return "已根据截断后的MCP结果完成。"

        await self.reconfigure_runtime(main_agent_stream_generator=agent_fixture)
        now = datetime(2026, 8, 29, 11, 0, 0)
        await self.runtime.storage.create_user_mcp_server(
            UserMCPServer(
                server_id="mcp-oversized",
                owner_user_id="acc-1",
                display_name="Oversized服务",
                routing_description="查询大结果",
                endpoint_url="https://mcp.example.test/rpc",
                transport=UserMCPTransport.STREAMABLE_HTTP,
                health_status=UserMCPHealthStatus.AVAILABLE,
                created_at=now,
                updated_at=now,
            )
        )
        executor = next(
            item
            for item in self.runtime._agent_capability_invoker._invocation._executor._executors
            if isinstance(item, MCPDispatchExecutor)
        )
        coordinator = executor._coordinator
        gateway = coordinator._gateway
        adapter = _ResultAdapter(
            sentinel + '"\\\n' * 10_000 + end_sentinel
        )
        endpoint_policy = EndpointPolicy(resolver=_PublicEndpointResolver())
        gateway._client_factory = lambda server, credentials, endpoint: adapter
        gateway._credential_loader = lambda server: {}
        gateway._endpoint_revalidator = lambda server: endpoint_policy.validate(
            server.endpoint_url
        )
        coordinator._selector = _CallThenFinishSelector(sentinel)
        coordinator._server_router = _ForbiddenRouter()
        input_schema = {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        }
        await self.runtime.storage.save_user_mcp_tool_grant(
            UserMCPToolGrant(
                grant_id="grant-mcp-oversized-e2e",
                owner_user_id="acc-1",
                server_id="mcp-oversized",
                tool_name="lookup",
                server_security_version=1,
                input_schema_sha256=hashlib.sha256(
                    json.dumps(
                        input_schema,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest(),
                granted_at=now,
            )
        )

        response = await self.client.post(
            "/api/v1/conversations/chat-messages",
            json={
                "conversation_id": "conv-mcp-oversized",
                "content": "查询大结果并说明结果不完整",
                "routing_mode": "force_capability",
                "capability_id": "mcp.dispatch",
                "metadata": {
                    "mcp_server_binding": {"server_id": "mcp-oversized"},
                    "deep_thinking": False,
                },
            },
        )

        self.assertEqual(response.status_code, 202, response.text)
        terminal = await self.wait_for_terminal_task(response.json()["task_id"])
        self.assertEqual(terminal["status"], "completed")
        self.assertEqual(adapter.call_count, 1)
        self.assertEqual(len(prompts), 1)
        self.assertIn(sentinel, prompts[0])
        self.assertNotIn(end_sentinel, prompts[0])
        self.assertIn('"truncated":true', prompts[0])


if __name__ == "__main__":
    import unittest

    unittest.main()
