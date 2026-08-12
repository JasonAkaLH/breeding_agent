from __future__ import annotations

import json
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx

from src.integrations.mcp.adapter_2026 import (
    MCP2026Adapter,
    MCPCompletedOutcome,
    MCPInputRequiredOutcome,
    MCPMethodNotFoundError,
    MCPTaskCreatedOutcome,
    MCPUnsupportedProtocolVersionError,
    safe_auto_downgrade_version,
)
from src.integrations.mcp.client import MCPAuthRequiredError, MCPClientError, MCPProtocolError
from src.integrations.mcp.protocol import MCPStreamEvent, MCPTransportResponse
from src.integrations.mcp.streaming_response import parse_json_rpc_byte_stream
from src.integrations.mcp.temporary_results import MCPTemporaryResultStore
from src.integrations.mcp.transport_http import StreamableHTTPTransport

FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "fixtures" / "mcp" / "messages" / "2026-07-28"


class FakeRequestScopedTransport:
    def __init__(self, responses: list[MCPTransportResponse]) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.closed = False

    async def send(
        self,
        message: Mapping[str, Any],
        *,
        protocol_version: str,
        request_headers: Mapping[str, str],
        timeout_seconds: float | None = None,
    ) -> MCPTransportResponse:
        self.requests.append(
            {
                "message": dict(message),
                "protocol_version": protocol_version,
                "request_headers": dict(request_headers),
                "timeout_seconds": timeout_seconds,
            }
        )
        return self.responses.pop(0)

    async def close(self) -> None:
        self.closed = True


class StreamingControlTransport(FakeRequestScopedTransport):
    def __init__(
        self,
        responses: list[MCPTransportResponse],
        streaming_message: Mapping[str, Any],
    ) -> None:
        super().__init__(responses)
        self.streaming_message = streaming_message

    async def send_streaming(
        self,
        message: Mapping[str, Any],
        *,
        protocol_version: str,
        request_headers: Mapping[str, str],
        result_sink,
        timeout_seconds: float | None = None,
    ) -> MCPTransportResponse:
        self.requests.append(
            {
                "message": dict(message),
                "protocol_version": protocol_version,
                "request_headers": dict(request_headers),
                "timeout_seconds": timeout_seconds,
            }
        )

        async def chunks():
            yield json.dumps(self.streaming_message, separators=(",", ":")).encode()

        parsed = await parse_json_rpc_byte_stream(
            chunks(),
            result_sink,
            control_result_types=frozenset({"input_required", "task"}),
        )
        return MCPTransportResponse(message=parsed.message)


def fixture(name: str) -> Mapping[str, Any]:
    return json.loads((FIXTURE_ROOT / name).read_text(encoding="utf-8"))


def response(name: str) -> MCPTransportResponse:
    payload = dict(fixture(name))
    if name == "server_discover_result.json":
        payload["id"] = 1
    return MCPTransportResponse(message=payload)


class MCP20260728AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_discover_and_list_are_stateless_and_capture_scope_local_cache_hints(self) -> None:
        list_result = fixture("tools_list_result.json")
        list_result["id"] = 2
        transport = FakeRequestScopedTransport([response("server_discover_result.json"), MCPTransportResponse(list_result)])
        adapter = MCP2026Adapter(server_id="crm", transport=transport)

        session = await adapter.initialize()
        page = await adapter.list_tools_page()

        self.assertEqual(session.negotiated_protocol_version, "2026-07-28")
        self.assertIsNone(session.session_id)
        self.assertEqual(page.cache_hint.ttl_ms, 30000)
        self.assertEqual(page.cache_hint.cache_scope, "private")
        self.assertEqual(page.tools[0]["name"], "lookup")
        self.assertEqual([item["message"]["method"] for item in transport.requests], ["server/discover", "tools/list"])
        for sent in transport.requests:
            params = sent["message"]["params"]
            self.assertEqual(params["_meta"]["io.modelcontextprotocol/protocolVersion"], "2026-07-28")
            self.assertEqual(sent["request_headers"]["MCP-Protocol-Version"], "2026-07-28")
            self.assertEqual(sent["request_headers"]["Mcp-Method"], sent["message"]["method"])
            self.assertNotIn("MCP-Session-Id", sent["request_headers"])
            self.assertNotIn("Last-Event-ID", sent["request_headers"])
        self.assertNotIn("initialize", [item["message"]["method"] for item in transport.requests])

    async def test_request_scoped_sse_accepts_notifications_then_one_final_response(self) -> None:
        completed = fixture("tools_list_result.json")
        completed["id"] = 3
        completed["result"] = {"resultType": "complete", "content": [{"type": "text", "text": "ok"}]}
        transport = FakeRequestScopedTransport(
            [
                response("server_discover_result.json"),
                response("tools_list_result.json"),
                MCPTransportResponse(
                    message=completed,
                    sse_events=(
                        MCPStreamEvent(message={"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progress": 1}}),
                        MCPStreamEvent(message=completed),
                    ),
                ),
            ]
        )
        adapter = MCP2026Adapter(server_id="crm", transport=transport)
        await adapter.initialize()
        await adapter.list_tools()

        outcome = await adapter.call_tool("lookup", {"tenant": "alpha"})

        self.assertIsInstance(outcome, MCPCompletedOutcome)
        self.assertEqual(len(adapter.last_stream_notifications), 1)
        self.assertIsNone(transport.requests[-1]["timeout_seconds"])

    async def test_mrtr_request_state_is_sealed_and_cleared_on_close(self) -> None:
        input_required = fixture("input_required_result.json")
        input_required["id"] = 3
        retry_complete = {"jsonrpc": "2.0", "id": 4, "result": {"resultType": "complete", "content": []}}
        transport = FakeRequestScopedTransport(
            [response("server_discover_result.json"), response("tools_list_result.json"), MCPTransportResponse(input_required), MCPTransportResponse(retry_complete)]
        )
        adapter = MCP2026Adapter(
            server_id="crm",
            transport=transport,
            enable_elicitation=True,
            safe_ref_factory=lambda prefix: f"{prefix}:safe",
        )
        await adapter.initialize()
        await adapter.list_tools()

        pending = await adapter.call_tool("lookup", {"tenant": "alpha"})
        self.assertIsInstance(pending, MCPInputRequiredOutcome)
        self.assertEqual(pending.sealed_request_state_ref, "mcp-request-state:safe")
        self.assertNotIn("opaque-server-state", repr(pending))
        completed = await adapter.call_tool(
            "lookup",
            {"tenant": "alpha"},
            input_responses={"confirm": {"action": "accept", "content": {"approved": True}}},
            sealed_request_state_ref=pending.sealed_request_state_ref,
        )
        self.assertIsInstance(completed, MCPCompletedOutcome)
        self.assertEqual(transport.requests[-1]["message"]["params"]["requestState"], "opaque-server-state")
        await adapter.close()
        with self.assertRaisesRegex(MCPProtocolError, "Unknown or expired"):
            adapter._resolve_request_state("mcp-request-state:safe")

    async def test_streaming_call_preserves_input_required_control_outcome(self) -> None:
        input_required = fixture("input_required_result.json")
        input_required["id"] = 3
        transport = StreamingControlTransport(
            [response("server_discover_result.json"), response("tools_list_result.json")],
            input_required,
        )
        adapter = MCP2026Adapter(
            server_id="crm",
            transport=transport,
            enable_elicitation=True,
            safe_ref_factory=lambda prefix: f"{prefix}:safe",
        )
        await adapter.initialize()
        await adapter.list_tools()
        with tempfile.TemporaryDirectory() as temporary:
            store = MCPTemporaryResultStore(
                Path(temporary), memory_threshold_bytes=4
            )
            pending = await adapter.call_tool(
                "lookup",
                {"tenant": "alpha"},
                result_sink=store.create_sink("task-control"),
            )

        self.assertIsInstance(pending, MCPInputRequiredOutcome)
        self.assertEqual(pending.sealed_request_state_ref, "mcp-request-state:safe")

    async def test_tasks_are_extension_gated_and_expose_only_safe_refs(self) -> None:
        create_task = fixture("create_task_result.json")
        create_task["id"] = 3
        task_state = {
            "jsonrpc": "2.0",
            "id": 4,
            "result": {
                "resultType": "complete",
                "taskId": "raw-remote-task-id",
                "status": "completed",
                "createdAt": "2026-08-12T00:00:00Z",
                "lastUpdatedAt": "2026-08-12T00:01:00Z",
                "ttlMs": 3600000,
                "result": {"content": []},
            },
        }
        transport = FakeRequestScopedTransport(
            [response("server_discover_result.json"), response("tools_list_result.json"), MCPTransportResponse(create_task), MCPTransportResponse(task_state)]
        )
        adapter = MCP2026Adapter(
            server_id="crm",
            transport=transport,
            enable_tasks=True,
            safe_ref_factory=lambda prefix: f"{prefix}:safe",
        )
        await adapter.initialize()
        await adapter.list_tools()

        created = await adapter.call_tool("lookup", {"tenant": "alpha"})
        self.assertIsInstance(created, MCPTaskCreatedOutcome)
        self.assertEqual(created.safe_remote_task_ref, "mcp-task:safe")
        self.assertNotIn("raw-remote-task-id", repr(created))
        state = await adapter.tasks_get(created.safe_remote_task_ref)
        self.assertTrue(state.terminal)
        self.assertEqual(state.status, "completed")
        self.assertEqual(transport.requests[-1]["message"]["params"]["taskId"], "raw-remote-task-id")
        self.assertEqual(transport.requests[-1]["request_headers"]["Mcp-Name"], "raw-remote-task-id")

    def test_safe_auto_downgrade_requires_explicit_unsupported_evidence(self) -> None:
        self.assertEqual(
            safe_auto_downgrade_version(
                MCPUnsupportedProtocolVersionError(
                    supported_versions=("2025-06-18", "2025-11-25"),
                    requested_version="2026-07-28",
                    request_method="server/discover",
                ),
                auto_mode=True,
            ),
            "2025-11-25",
        )
        self.assertEqual(
            safe_auto_downgrade_version(
                MCPMethodNotFoundError("missing", request_method="server/discover"),
                auto_mode=True,
            ),
            "2025-11-25",
        )
        self.assertIsNone(
            safe_auto_downgrade_version(
                MCPMethodNotFoundError("missing", request_method="tools/call"),
                auto_mode=True,
            )
        )
        for error in (
            MCPAuthRequiredError(),
            MCPClientError("network", code="mcp_transport_error", retriable=True),
            MCPProtocolError("malformed"),
        ):
            with self.subTest(error=type(error).__name__):
                self.assertIsNone(safe_auto_downgrade_version(error, auto_mode=True))
        self.assertIsNone(
            safe_auto_downgrade_version(
                MCPMethodNotFoundError("missing", request_method="server/discover"),
                auto_mode=False,
            )
        )

    async def test_http_400_structured_unsupported_error_is_available_to_auto_negotiation(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.headers["MCP-Protocol-Version"], "2026-07-28")
            return httpx.Response(400, json=fixture("unsupported_protocol_error.json"))

        http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        transport = StreamableHTTPTransport(endpoint="https://mcp.example.test/rpc", client=http_client)
        adapter = MCP2026Adapter(server_id="crm", transport=transport)
        try:
            with self.assertRaises(MCPUnsupportedProtocolVersionError) as raised:
                await adapter.discover()
            self.assertEqual(safe_auto_downgrade_version(raised.exception, auto_mode=True), "2025-11-25")
        finally:
            await http_client.aclose()


if __name__ == "__main__":
    unittest.main()
