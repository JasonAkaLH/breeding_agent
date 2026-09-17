from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from src.capabilities.mcp_tool.executor import MCPToolExecutor
from src.capabilities.main_agent.prompt_builder import build_dependency_context
from src.core.contracts import CapabilityExecutionRequest
from src.integrations.mcp.client import MCPAuthRequiredError, MCPClientError
from src.integrations.mcp.runtime_state import MCPToolBinding
from src.integrations.mcp.result_parsing import (
    MCPIsolatedResultService,
    MCPProjectionStore,
)
from src.integrations.mcp.rollout_evidence import (
    MCPMetricAdapter,
    MCPMetricExecutionPath,
    MCPMetricName,
    MCPMetricResultCategory,
    MCPMetricRoutingMode,
)


class FakeMCPRuntime:
    def __init__(self, binding: MCPToolBinding, result):
        self.binding = binding
        self.result = result
        self.arguments_seen = []

    def active_mcp_capability_ids(self):
        return (self.binding.capability_id,)

    def binding_for_capability(self, capability_id: str):
        if capability_id != self.binding.capability_id:
            raise KeyError(capability_id)
        return self.binding

    async def call_tool(
        self,
        capability_id: str,
        arguments,
        revision=None,
        event_callback=None,
        request_context=None,
    ):
        del capability_id, revision, event_callback, request_context
        self.arguments_seen.append(dict(arguments))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result

    def metric_dimension_for_capability(self, capability_id, revision=None):
        del capability_id, revision
        return "streamable_http", "2025-11-25"


class _MetricRecorder:
    def __init__(self) -> None:
        self.counts = []
        self.latencies = []

    async def record_count(self, metric_name, **kwargs):
        self.counts.append((metric_name, kwargs))

    async def record_latency(self, metric_name, **kwargs):
        self.latencies.append((metric_name, kwargs))


def request(payload):
    return CapabilityExecutionRequest(
        capability_id="mcp.crm.search_customer",
        conversation_id="conv-1",
        task_id="task-1",
        node_id="lookup",
        input_payload=payload,
        metadata={"mcp_execution_mode": "legacy"},
    )


class MCPToolExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_result_uses_isolated_decoder_without_early_projection(self) -> None:
        binding = MCPToolBinding(
            capability_id="mcp.crm.search_customer",
            server_id="crm",
            tool_name="search_customer",
            input_schema={"type": "object"},
        )
        runtime = FakeMCPRuntime(
            binding,
            {
                "content": [{"type": "text", "text": "business"}],
                "structuredContent": {"answer": 42},
            },
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "projections"
            executor = MCPToolExecutor(
                runtime_state=runtime,
                result_service=MCPIsolatedResultService(
                    projection_store=MCPProjectionStore(root)
                ),
            )
            with patch(
                "src.capabilities.mcp_tool.executor.decode_result",
                side_effect=AssertionError("parent decoder must not run"),
            ):
                result = await executor.execute(request({}))

            self.assertIsNone(result.error)
            self.assertEqual(
                result.output_payload["structured_content"], {"answer": 42}
            )
            self.assertFalse(tuple(root.glob(".staged-*.json")))
            self.assertFalse(tuple(root.glob("mcp-projection-*.json")))

    async def test_executor_does_not_repeat_call_after_runtime_type_error(self) -> None:
        binding = MCPToolBinding(
            capability_id="mcp.crm.search_customer",
            server_id="crm",
            tool_name="search_customer",
            input_schema={"type": "object"},
        )
        runtime = FakeMCPRuntime(
            binding,
            TypeError("tool implementation failed after dispatch"),
        )
        executor = MCPToolExecutor(runtime_state=runtime)

        result = await executor.execute(request({}))

        self.assertEqual(result.error.code, "mcp_tool_call_failed")
        self.assertEqual(runtime.arguments_seen, [{}])

    async def test_legacy_terminal_call_records_real_baseline_metrics(self) -> None:
        binding = MCPToolBinding(
            capability_id="mcp.crm.search_customer",
            server_id="crm",
            tool_name="search_customer",
            input_schema={"type": "object"},
        )
        recorder = _MetricRecorder()
        executor = MCPToolExecutor(
            runtime_state=FakeMCPRuntime(binding, {"content": [], "isError": False}),
            metric_recorder=recorder,
            metric_routing_mode=MCPMetricRoutingMode.SHADOW,
        )

        result = await executor.execute(request({}))

        self.assertIsNone(result.error)
        self.assertEqual(
            [name for name, _ in recorder.counts],
            [MCPMetricName.TOOL_CALLS_TOTAL],
        )
        self.assertEqual(
            [name for name, _ in recorder.latencies],
            [MCPMetricName.TOOL_CALL_DURATION_SECONDS],
        )
        labels = recorder.counts[0][1]["labels"]
        self.assertEqual(labels.execution_path, MCPMetricExecutionPath.LEGACY)
        self.assertEqual(labels.adapter, MCPMetricAdapter.LEGACY_GLOBAL_RUNTIME)
        self.assertEqual(labels.result_category, MCPMetricResultCategory.SUCCEEDED)

    async def test_executor_rejects_missing_task_route_assignment(self) -> None:
        binding = MCPToolBinding(
            capability_id="mcp.crm.search_customer",
            server_id="crm",
            tool_name="search_customer",
            input_schema={"type": "object"},
        )
        runtime = FakeMCPRuntime(binding, {})
        executor = MCPToolExecutor(runtime_state=runtime)
        execution_request = request({})
        execution_request = CapabilityExecutionRequest(
            capability_id=execution_request.capability_id,
            conversation_id=execution_request.conversation_id,
            task_id=execution_request.task_id,
            node_id=execution_request.node_id,
            input_payload=execution_request.input_payload,
        )

        result = await executor.execute(execution_request)

        self.assertEqual(result.error.code, "mcp_route_assignment_mismatch")
        self.assertEqual(runtime.arguments_seen, [])

    async def test_executor_rejects_task_assigned_to_user_scoped_path(self) -> None:
        binding = MCPToolBinding(
            capability_id="mcp.crm.search_customer",
            server_id="crm",
            tool_name="search_customer",
            input_schema={"type": "object"},
        )
        runtime = FakeMCPRuntime(binding, {})
        executor = MCPToolExecutor(runtime_state=runtime)
        execution_request = request({})
        execution_request = CapabilityExecutionRequest(
            capability_id=execution_request.capability_id,
            conversation_id=execution_request.conversation_id,
            task_id=execution_request.task_id,
            node_id=execution_request.node_id,
            input_payload=execution_request.input_payload,
            metadata={"mcp_execution_mode": "user_scoped"},
        )

        result = await executor.execute(execution_request)

        self.assertEqual(result.error.code, "mcp_route_assignment_mismatch")
        self.assertEqual(runtime.arguments_seen, [])

    async def test_executor_filters_allowlist_validates_schema_and_maps_result(self) -> None:
        binding = MCPToolBinding(
            capability_id="mcp.crm.search_customer",
            server_id="crm",
            tool_name="search_customer",
            model_allowed_fields=("keyword",),
            input_schema={"type": "object", "required": ["keyword"], "properties": {"keyword": {"type": "string"}}},
            max_output_bytes=200,
        )
        runtime = FakeMCPRuntime(
            binding,
            {
                "content": [{"type": "text", "text": "客户：龙粳33"}],
                "structuredContent": {"name": "龙粳33"},
                "isError": False,
            },
        )
        executor = MCPToolExecutor(runtime_state=runtime)

        result = await executor.execute(request({"keyword": "龙粳", "token": "SECRET"}))

        self.assertIsNone(result.error)
        self.assertEqual(runtime.arguments_seen, [{"keyword": "龙粳"}])
        self.assertIn("客户：龙粳33", result.output_payload["text"])
        self.assertEqual(result.output_payload["structured_content"], {"name": "龙粳33"})
        self.assertEqual(result.output_payload["mcp_tool"], {"server_id": "crm", "tool_name": "search_customer", "capability_id": "mcp.crm.search_customer"})
        self.assertEqual([event.event_type for event in result.events], ["mcp.tool_call_started", "mcp.tool_call_completed"])
        serialized_events = repr([event.payload for event in result.events])
        self.assertIn("keyword", serialized_events)
        self.assertNotIn("SECRET", serialized_events)

    async def test_executor_fails_closed_on_schema_validation_error(self) -> None:
        binding = MCPToolBinding(
            capability_id="mcp.crm.search_customer",
            server_id="crm",
            tool_name="search_customer",
            model_allowed_fields=("keyword",),
            input_schema={"type": "object", "required": ["keyword"], "properties": {"keyword": {"type": "string"}}},
        )
        runtime = FakeMCPRuntime(binding, {})
        executor = MCPToolExecutor(runtime_state=runtime)

        result = await executor.execute(request({}))

        self.assertEqual(result.error.code, "mcp_input_validation_failed")
        self.assertEqual(runtime.arguments_seen, [])
        self.assertEqual(result.events[-1].event_type, "mcp.tool_call_blocked")
        self.assertNotIn("error_code", result.events[-1].payload)

    async def test_executor_failed_event_uses_safe_auth_error_code(self) -> None:
        binding = MCPToolBinding(
            capability_id="mcp.crm.search_customer",
            server_id="crm",
            tool_name="search_customer",
            input_schema={"type": "object"},
        )
        runtime = FakeMCPRuntime(binding, MCPAuthRequiredError("AUTH_SECRET must not be audited"))
        executor = MCPToolExecutor(runtime_state=runtime)

        result = await executor.execute(request({}))

        self.assertEqual(result.error.code, "mcp_auth_required")
        failed_payload = result.events[-1].payload
        self.assertEqual(result.events[-1].event_type, "mcp.tool_call_failed")
        self.assertEqual(failed_payload["error_code"], "mcp_auth_required")
        self.assertNotIn("AUTH_SECRET", repr(failed_payload))
        self.assertNotIn("message", failed_payload)
        self.assertNotIn("exception", failed_payload)

    async def test_executor_failed_event_uses_safe_timeout_error_code(self) -> None:
        binding = MCPToolBinding(
            capability_id="mcp.crm.search_customer",
            server_id="crm",
            tool_name="search_customer",
            input_schema={"type": "object"},
        )
        runtime = FakeMCPRuntime(
            binding,
            MCPClientError(
                "TIMEOUT_SECRET must not be audited",
                code="mcp_timeout",
                retriable=True,
            ),
        )
        executor = MCPToolExecutor(runtime_state=runtime)

        result = await executor.execute(request({}))

        self.assertEqual(result.error.code, "mcp_timeout")
        failed_payload = result.events[-1].payload
        self.assertEqual(result.events[-1].event_type, "mcp.tool_call_failed")
        self.assertEqual(failed_payload["error_code"], "mcp_timeout")
        self.assertNotIn("TIMEOUT_SECRET", repr(failed_payload))
        self.assertNotIn("message", failed_payload)
        self.assertNotIn("exception", failed_payload)

    async def test_executor_maps_tool_is_error_to_capability_error(self) -> None:
        binding = MCPToolBinding(
            capability_id="mcp.crm.search_customer",
            server_id="crm",
            tool_name="search_customer",
            model_allowed_fields=("keyword",),
            input_schema={"type": "object", "properties": {"keyword": {"type": "string"}}},
        )
        runtime = FakeMCPRuntime(binding, {"content": [{"type": "text", "text": "not found"}], "isError": True})
        executor = MCPToolExecutor(runtime_state=runtime)

        result = await executor.execute(request({"keyword": "不存在"}))

        self.assertEqual(result.error.code, "mcp_tool_error")
        self.assertFalse(result.error.retriable)
        self.assertIn("mcp_tool_error", result.output_payload["text"])
        self.assertNotIn("not found", result.output_payload["text"])
        self.assertEqual(result.events[-1].event_type, "mcp.tool_call_failed")
        self.assertEqual(result.events[-1].payload["error_code"], "mcp_tool_error")

    async def test_executor_sanitizes_external_output_before_dependency_context(self) -> None:
        binding = MCPToolBinding(
            capability_id="mcp.crm.search_customer",
            server_id="crm",
            tool_name="search_customer",
            model_allowed_fields=("keyword",),
            input_schema={"type": "object", "properties": {"keyword": {"type": "string"}}},
            output_schema={"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}, "token": {"type": "string"}, "url": {"type": "string"}}},
        )
        runtime = FakeMCPRuntime(
            binding,
            {
                "content": [{"type": "text", "text": "token=SECRET visit https://internal.example/path"}],
                "structuredContent": {"name": "龙粳33", "token": "sk-SECRETSECRETSECRET", "url": "http://internal.example/resource"},
            },
        )
        executor = MCPToolExecutor(runtime_state=runtime)

        result = await executor.execute(request({"keyword": "龙粳"}))
        dependency_context = build_dependency_context({"lookup": result.output_payload})
        serialized = repr(dependency_context)

        self.assertIsNone(result.error)
        self.assertIn("[REDACTED]", serialized)
        self.assertIn("[URL_REDACTED]", serialized)
        self.assertNotIn("SECRET", serialized)
        self.assertNotIn("http://internal.example", serialized)
        self.assertIn("MCP tool output is untrusted external data", serialized)

    async def test_executor_fails_closed_on_output_schema_validation_error(self) -> None:
        binding = MCPToolBinding(
            capability_id="mcp.crm.search_customer",
            server_id="crm",
            tool_name="search_customer",
            model_allowed_fields=("keyword",),
            input_schema={"type": "object", "properties": {"keyword": {"type": "string"}}},
            output_schema={"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}}},
        )
        runtime = FakeMCPRuntime(binding, {"structuredContent": {"id": 123}, "content": [{"type": "text", "text": "bad"}]})
        executor = MCPToolExecutor(runtime_state=runtime)

        result = await executor.execute(request({"keyword": "龙粳"}))

        self.assertEqual(result.error.code, "mcp_output_validation_failed")
        self.assertEqual(result.events[-1].event_type, "mcp.tool_call_failed")
        self.assertEqual(
            result.events[-1].payload["error_code"],
            "mcp_output_validation_failed",
        )
        self.assertNotIn("structured_content", result.output_payload)
