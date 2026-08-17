from __future__ import annotations

import json
import tempfile
import unittest
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from src.core.enums import NodeStatus, UserMCPTransport
from src.core.models import (
    Conversation,
    MCPBranchRecord,
    MCPCallRecord,
    Task,
    TaskNode,
    UserMCPServer,
)
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
from src.integrations.mcp.credentials import (
    CredentialSecurityError,
    EncryptedCredential,
    MAX_REMOTE_TASK_ID_BYTES,
    MAX_REQUEST_STATE_BYTES,
    MAX_TASK_PRIVATE_JSON_BYTES,
    MCPRecoveryCallContext,
    MCPRecoveryService,
)
from tests.master_key_support import recovery_cipher
from src.integrations.mcp.endpoint_policy import EndpointPolicy
from src.integrations.mcp.protocol import MCPStreamEvent, MCPTransportResponse
from src.integrations.mcp.recovery_worker import MCPRemoteTaskRecoveryWorker
from src.integrations.mcp.streaming_response import parse_json_rpc_byte_stream
from src.integrations.mcp.temporary_results import MCPTemporaryResultStore
from src.integrations.mcp.transport_http import StreamableHTTPTransport
from src.integrations.mcp.user_client import UserMCPClientFactory
from src.storage.sqlite import (
    SQLiteStorage,
    bootstrap_sqlite_database,
    create_sqlite_engine,
    create_sqlite_session_factory,
)

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
    async def test_recovery_factory_pins_durable_2026_protocol_for_auto_server(self) -> None:
        class _Resolver:
            def resolve(self, _hostname: str, _port: int) -> tuple[str, ...]:
                return ("8.8.8.8",)

        factory = UserMCPClientFactory(EndpointPolicy(resolver=_Resolver()))
        server = UserMCPServer(
            server_id="crm",
            owner_user_id="alice",
            display_name="CRM",
            routing_description="CRM",
            endpoint_url="https://example.test/mcp",
            transport=UserMCPTransport.STREAMABLE_HTTP,
        )
        endpoint = await factory.revalidate_endpoint(server)

        client = await factory.create_task_recovery(
            server,
            {},
            endpoint,
            protocol_version="2026-07-28",
        )

        self.assertIsInstance(client, MCP2026Adapter)
        self.assertTrue(client._recovery_only)
        await client.close()
        with self.assertRaisesRegex(
            MCPProtocolError, "mcp_remote_task_protocol_handler_unavailable"
        ):
            await factory.create_task_recovery(
                server,
                {},
                endpoint,
                protocol_version="2025-11-25",
            )

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

        self.assertFalse(adapter.supports_durable_recovery_context)
        with self.assertRaisesRegex(MCPProtocolError, "Durable MCP recovery is unavailable"):
            await adapter.call_tool("lookup", {"tenant": "alpha"})

    async def test_mrtr_request_state_survives_fresh_adapter_without_exposing_raw_state(self) -> None:
        input_required = fixture("input_required_result.json")
        input_required["id"] = 3
        retry_complete = {
            "jsonrpc": "2.0",
            "id": 3,
            "result": {"resultType": "complete", "content": []},
        }
        with tempfile.TemporaryDirectory() as temporary:
            engine = create_sqlite_engine(Path(temporary) / "recovery.sqlite3")
            bootstrap_sqlite_database(engine)
            session_factory = create_sqlite_session_factory(engine)
            first_storage = SQLiteStorage(session_factory)
            recovery = MCPRecoveryService(first_storage, recovery_cipher(b"r" * 32))
            first_transport = FakeRequestScopedTransport(
                [
                    response("server_discover_result.json"),
                    response("tools_list_result.json"),
                    MCPTransportResponse(input_required),
                ]
            )
            first = MCP2026Adapter(
                server_id="crm",
                transport=first_transport,
                enable_elicitation=True,
                safe_ref_factory=lambda prefix: f"{prefix}:durable",
                recovery_service=recovery,
            )
            await first.initialize()
            await first.list_tools()

            pending = await first.call_tool(
                "lookup",
                {"tenant": "alpha"},
                recovery_context=MCPRecoveryCallContext(
                    owner_user_id="alice",
                    task_id="task-1",
                    node_id="node-1",
                    call_ref="call-1",
                ),
            )
            await first.close()

            self.assertIsInstance(pending, MCPInputRequiredOutcome)
            saved = await first_storage.get_mcp_sealed_state(
                "alice", "task-1", pending.sealed_request_state_ref
            )
            self.assertIsNotNone(saved)
            self.assertNotIn(b"opaque-server-state", saved.ciphertext)
            self.assertNotIn("opaque-server-state", repr(saved))

            second_storage = SQLiteStorage(session_factory)
            second_transport = FakeRequestScopedTransport(
                [
                    response("server_discover_result.json"),
                    response("tools_list_result.json"),
                    MCPTransportResponse(retry_complete),
                ]
            )
            second = MCP2026Adapter(
                server_id="crm",
                transport=second_transport,
                enable_elicitation=True,
                recovery_service=MCPRecoveryService(
                    second_storage, recovery_cipher(b"r" * 32)
                ),
            )
            await second.initialize()
            await second.list_tools()
            self.assertTrue(second.supports_durable_recovery_context)
            for context in (
                MCPRecoveryCallContext("alice", "task-1", "node-2", "call-1"),
                MCPRecoveryCallContext("alice", "task-1", "node-1", "call-2"),
            ):
                with self.subTest(context=context), self.assertRaisesRegex(
                    MCPProtocolError, "Unknown or expired"
                ):
                    await second.call_tool(
                        "lookup",
                        {"tenant": "alpha"},
                        input_responses={"confirm": {"action": "accept"}},
                        sealed_request_state_ref=pending.sealed_request_state_ref,
                        recovery_context=context,
                    )
            completed = await second.call_tool(
                "lookup",
                {"tenant": "alpha"},
                input_responses={"confirm": {"action": "accept"}},
                sealed_request_state_ref=pending.sealed_request_state_ref,
                recovery_context=MCPRecoveryCallContext(
                    owner_user_id="alice",
                    task_id="task-1",
                    node_id="node-1",
                    call_ref="call-1",
                ),
            )

            self.assertIsInstance(completed, MCPCompletedOutcome)
            self.assertEqual(
                second_transport.requests[-1]["message"]["params"]["requestState"],
                "opaque-server-state",
            )
            engine.dispose()

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
            with self.assertRaisesRegex(
                MCPProtocolError, "Durable MCP recovery is unavailable"
            ):
                await adapter.call_tool(
                    "lookup",
                    {"tenant": "alpha"},
                    result_sink=store.create_sink("task-control"),
                )

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

        self.assertFalse(adapter.supports_durable_recovery_context)
        with self.assertRaisesRegex(MCPProtocolError, "Durable MCP recovery is unavailable"):
            await adapter.call_tool("lookup", {"tenant": "alpha"})

    async def test_remote_task_binding_survives_fresh_adapter_and_only_queries(self) -> None:
        create_task = fixture("create_task_result.json")
        create_task["id"] = 3
        task_state = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "resultType": "complete",
                "taskId": "raw-remote-task-id",
                "status": "completed",
                "createdAt": "2026-08-12T00:00:00Z",
                "lastUpdatedAt": "2026-08-12T00:01:00Z",
                "result": {"content": []},
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            engine = create_sqlite_engine(Path(temporary) / "tasks.sqlite3")
            bootstrap_sqlite_database(engine)
            session_factory = create_sqlite_session_factory(engine)
            first_storage = SQLiteStorage(session_factory)
            first = MCP2026Adapter(
                server_id="crm",
                transport=FakeRequestScopedTransport(
                    [
                        response("server_discover_result.json"),
                        response("tools_list_result.json"),
                        MCPTransportResponse(create_task),
                    ]
                ),
                enable_tasks=True,
                safe_ref_factory=lambda prefix: f"{prefix}:durable",
                recovery_service=MCPRecoveryService(
                    first_storage, recovery_cipher(b"t" * 32)
                ),
            )
            await first.initialize()
            await first.list_tools()
            created = await first.call_tool(
                "lookup",
                {"tenant": "alpha"},
                recovery_context=MCPRecoveryCallContext(
                    owner_user_id="alice",
                    task_id="task-1",
                    node_id="node-1",
                    call_ref="call-1",
                ),
            )
            await first.close()

            self.assertIsInstance(created, MCPTaskCreatedOutcome)
            binding = await first_storage.get_mcp_remote_task_binding(
                "alice", "task-1", created.safe_remote_task_ref
            )
            self.assertIsNotNone(binding)
            self.assertNotIn(b"raw-remote-task-id", binding.remote_task_ciphertext)
            self.assertNotIn("raw-remote-task-id", repr(binding))

            second_transport = FakeRequestScopedTransport(
                [
                    MCPTransportResponse(task_state),
                ]
            )
            second = MCP2026Adapter(
                server_id="crm",
                transport=second_transport,
                enable_tasks=True,
                recovery_service=MCPRecoveryService(
                    SQLiteStorage(session_factory), recovery_cipher(b"t" * 32)
                ),
                recovery_only=True,
            )
            with self.assertRaisesRegex(
                MCPProtocolError,
                "recovery-only client permits only task query/control methods",
            ):
                await second.initialize()
            self.assertEqual(second_transport.requests, [])
            for context in (
                MCPRecoveryCallContext("alice", "task-1", "node-2", "call-1"),
                MCPRecoveryCallContext("alice", "task-1", "node-1", "call-2"),
            ):
                with self.subTest(context=context), self.assertRaisesRegex(
                    MCPProtocolError, "Unknown or expired"
                ):
                    await second.tasks_get(
                        created.safe_remote_task_ref,
                        recovery_context=context,
                    )
            state = await second.tasks_get(
                created.safe_remote_task_ref,
                recovery_context=MCPRecoveryCallContext(
                    "alice", "task-1", "node-1", "call-1"
                ),
            )

            self.assertTrue(state.terminal)
            self.assertEqual(
                [request["message"]["method"] for request in second_transport.requests],
                ["tasks/get"],
            )
            engine.dispose()

    async def test_immediate_terminal_create_task_is_queried_once_and_persists_2026_result(self) -> None:
        now = datetime(2026, 8, 13, 12, 0, 0)
        expected = {
            "completed": ("completed", None, True),
            "failed": ("failed", "mcp_remote_task_failed", False),
            "cancelled": ("cancelled", "mcp_remote_task_cancelled", False),
        }
        with tempfile.TemporaryDirectory() as temporary:
            db_path = Path(temporary) / "terminal-tasks.sqlite3"
            engine = create_sqlite_engine(db_path)
            bootstrap_sqlite_database(engine)
            session_factory = create_sqlite_session_factory(engine)
            storage = SQLiteStorage(session_factory)
            recovery = MCPRecoveryService(
                storage, recovery_cipher(b"z" * 32), now_fn=lambda: now
            )
            await storage.save_conversation(Conversation("conv-terminal", "alice"))
            execution_transports: dict[str, FakeRequestScopedTransport] = {}
            safe_refs: dict[str, str] = {}

            for status in expected:
                task_id = f"task-terminal-{status}"
                node_id = f"node-terminal-{status}"
                call_ref = f"call-terminal-{status}"
                branch_id = f"branch-terminal-{status}"
                await storage.save_task(
                    Task(task_id, "conv-terminal", f"message-{status}")
                )
                await storage.save_mcp_branch_record(
                    MCPBranchRecord(
                        branch_id=branch_id,
                        owner_user_id="alice",
                        task_id=task_id,
                        node_id=node_id,
                        status="ready",
                        created_at=now,
                        updated_at=now,
                    )
                )
                self.assertTrue(
                    await storage.reserve_mcp_call(
                        MCPCallRecord(
                            call_ref=call_ref,
                            branch_id=branch_id,
                            owner_user_id="alice",
                            task_id=task_id,
                            node_id=node_id,
                            server_id="crm",
                            tool_name="lookup",
                            status="active",
                            call_sequence=1,
                            arguments_sha256="args-sha",
                            server_security_version=1,
                            input_schema_sha256="schema-sha",
                            protocol_version="2026-07-28",
                            may_have_dispatched=True,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                )
                create_task = dict(fixture("create_task_result.json"))
                create_task["id"] = 3
                create_task["result"] = {
                    **dict(create_task["result"]),
                    "taskId": f"raw-terminal-{status}",
                    "status": status,
                }
                transport = FakeRequestScopedTransport(
                    [
                        response("server_discover_result.json"),
                        response("tools_list_result.json"),
                        MCPTransportResponse(create_task),
                    ]
                )
                execution_transports[status] = transport
                adapter = MCP2026Adapter(
                    server_id="crm",
                    transport=transport,
                    enable_tasks=True,
                    safe_ref_factory=lambda prefix, status=status: (
                        f"{prefix}:terminal-{status}"
                    ),
                    recovery_service=recovery,
                )
                await adapter.initialize()
                await adapter.list_tools()
                created = await adapter.call_tool(
                    "lookup",
                    {"tenant": "alpha"},
                    recovery_context=MCPRecoveryCallContext(
                        "alice",
                        task_id,
                        node_id,
                        call_ref,
                        continuation_plan={
                            "task_id": task_id,
                            "nodes": [
                                {
                                    "node_id": node_id,
                                    "capability_id": "mcp.dispatch",
                                }
                            ],
                        },
                    ),
                )
                self.assertIsInstance(created, MCPTaskCreatedOutcome)
                safe_refs[status] = created.safe_remote_task_ref
                binding = await storage.get_mcp_remote_task_binding(
                    "alice", task_id, created.safe_remote_task_ref
                )
                self.assertIsNotNone(binding)
                assert binding is not None
                self.assertEqual(binding.last_status, status)
                self.assertIsNone(binding.next_poll_at)
                self.assertIsNone(binding.terminal_at)
                self.assertEqual(binding.continuation_plan["task_id"], task_id)
                await storage.save_task_node(
                    TaskNode(
                        node_id=node_id,
                        task_id=task_id,
                        capability_id="mcp.dispatch",
                        status=NodeStatus.WAITING_FOR_DEPENDENCY,
                    )
                )
                await storage.publish_mcp_remote_task_binding(
                    "alice", task_id, created.safe_remote_task_ref, published_at=now
                )
                published = await storage.get_mcp_remote_task_binding(
                    "alice", task_id, created.safe_remote_task_ref
                )
                self.assertEqual(published.continuation_plan["task_id"], task_id)
                self.assertEqual(
                    [
                        request["message"]["method"]
                        for request in transport.requests
                    ].count("tools/call"),
                    1,
                )

            engine.dispose()
            engine = create_sqlite_engine(db_path)
            bootstrap_sqlite_database(engine)
            storage = SQLiteStorage(create_sqlite_session_factory(engine))
            recovery = MCPRecoveryService(
                storage,
                recovery_cipher(b"z" * 32),
                now_fn=lambda: now + timedelta(seconds=1),
            )
            recovery_transports: dict[str, FakeRequestScopedTransport] = {}
            for status, safe_ref in safe_refs.items():
                task_result: dict[str, Any] = {
                    "resultType": "complete",
                    "taskId": f"raw-terminal-{status}",
                    "status": status,
                    "createdAt": "2026-08-13T12:00:00Z",
                    "lastUpdatedAt": "2026-08-13T12:00:01Z",
                }
                if status == "completed":
                    task_result["result"] = {
                        "content": [{"type": "text", "text": "final-2026"}],
                        "structuredContent": {"status": "complete"},
                    }
                recovery_transports[safe_ref] = FakeRequestScopedTransport(
                    [
                        MCPTransportResponse(
                            {
                                "jsonrpc": "2.0",
                                "id": 1,
                                "result": task_result,
                            }
                        )
                    ]
                )

            persisted_results: dict[str, Mapping[str, Any]] = {}

            async def persist_result(binding, result: Mapping[str, Any]) -> str:
                persisted_results[binding.safe_remote_task_ref] = dict(result)
                return f"result:{binding.safe_remote_task_ref}"

            worker = MCPRemoteTaskRecoveryWorker(
                storage=storage,
                client_factory=lambda binding: MCP2026Adapter(
                    server_id="crm",
                    transport=recovery_transports[binding.safe_remote_task_ref],
                    enable_tasks=True,
                    recovery_service=recovery,
                    recovery_only=True,
                ),
                instance_id="worker-2026-immediate-terminal",
                result_persister=persist_result,
                now_fn=lambda: now + timedelta(seconds=1),
            )
            self.assertEqual(await worker.run_once(), 3)
            for status, (call_status, error_code, has_result) in expected.items():
                methods = [
                    request["message"]["method"]
                    for request in recovery_transports[safe_refs[status]].requests
                ]
                self.assertEqual(methods, ["tasks/get"])
                self.assertNotIn("tools/call", methods)
                call = await storage.get_mcp_call_record(
                    "alice", f"task-terminal-{status}", f"call-terminal-{status}"
                )
                self.assertIsNotNone(call)
                assert call is not None
                self.assertEqual(call.status, call_status)
                self.assertEqual(call.safe_error_code, error_code)
                self.assertEqual(
                    call.result_ref,
                    f"result:{safe_refs[status]}" if has_result else None,
                )
            self.assertEqual(
                persisted_results[safe_refs["completed"]]["structuredContent"],
                {"status": "complete"},
            )
            engine.dispose()

    async def test_unknown_2026_create_task_status_fails_closed_after_one_call(self) -> None:
        create_task = dict(fixture("create_task_result.json"))
        create_task["id"] = 3
        create_task["result"] = {
            **dict(create_task["result"]),
            "status": "unknown",
        }
        transport = FakeRequestScopedTransport(
            [
                response("server_discover_result.json"),
                response("tools_list_result.json"),
                MCPTransportResponse(create_task),
            ]
        )
        adapter = MCP2026Adapter(
            server_id="crm",
            transport=transport,
            enable_tasks=True,
            recovery_service=MCPRecoveryService(object(), recovery_cipher(b"u" * 32)),
        )
        await adapter.initialize()
        await adapter.list_tools()

        with self.assertRaisesRegex(MCPProtocolError, "Task status is invalid"):
            await adapter.call_tool(
                "lookup",
                {"tenant": "alpha"},
                recovery_context=MCPRecoveryCallContext(
                    "alice", "task-unknown", "node-unknown", "call-unknown"
                ),
            )
        self.assertEqual(
            [request["message"]["method"] for request in transport.requests].count(
                "tools/call"
            ),
            1,
        )

    def test_task_private_payload_size_caps_before_encrypt_and_after_decrypt(self) -> None:
        cipher = recovery_cipher(b"s" * 32)
        context = {
            "owner_user_id": "alice",
            "task_id": "task-1",
            "node_id": "node-1",
            "call_ref": "call-1",
            "state_kind": "request_state",
            "server_id": "crm",
            "protocol_version": "2026-07-28",
        }
        with self.assertRaisesRegex(
            CredentialSecurityError, "mcp_task_private_payload_too_large"
        ):
            cipher.seal_task_private_payload(
                **context,
                payload={"request_state": "x" * MAX_TASK_PRIVATE_JSON_BYTES},
            )

        valid = cipher.seal_task_private_payload(**context, payload={"value": "ok"})
        oversized = EncryptedCredential(
            nonce=valid.nonce,
            ciphertext=b"x" * (MAX_TASK_PRIVATE_JSON_BYTES + 17),
        )
        with self.assertRaisesRegex(
            CredentialSecurityError, "mcp_task_private_decryption_failed"
        ):
            cipher.unseal_task_private_payload(oversized, **context)

    async def test_recovery_rejects_oversized_raw_continuation_values(self) -> None:
        recovery = MCPRecoveryService(object(), recovery_cipher(b"v" * 32))
        context = MCPRecoveryCallContext("alice", "task-1", "node-1", "call-1")
        with self.assertRaisesRegex(
            CredentialSecurityError, "mcp_request_state_too_large"
        ):
            await recovery.save_request_state(
                context,
                server_id="crm",
                protocol_version="2026-07-28",
                sealed_state_ref="safe-state",
                request_state="界" * (MAX_REQUEST_STATE_BYTES // 3 + 1),
                tool_name="lookup",
                arguments={"tenant": "alpha"},
            )
        with self.assertRaisesRegex(
            CredentialSecurityError, "mcp_remote_task_id_too_large"
        ):
            await recovery.save_remote_task(
                context,
                server_id="crm",
                protocol_version="2026-07-28",
                safe_remote_task_ref="safe-task",
                remote_task_id="界" * (MAX_REMOTE_TASK_ID_BYTES // 3 + 1),
                status="working",
                poll_interval_ms=1000,
            )

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
