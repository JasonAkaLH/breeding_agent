from __future__ import annotations

import tempfile
import unittest
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from src.core.enums import NodeStatus, UserMCPProtocolPreference, UserMCPTransport
from src.core.models import (
    Conversation,
    MCPBranchRecord,
    MCPCallRecord,
    Task,
    TaskNode,
    UserMCPServer,
)
from src.integrations.mcp.adapter_2025_tasks import (
    MCP2025TaskCreatedOutcome,
    MCP2025TaskRecoveryClient,
    MCP2025TasksAdapter,
)
from src.integrations.mcp.client import MCPProtocolError
from src.integrations.mcp.credentials import MCPRecoveryCallContext, MCPRecoveryService
from src.integrations.mcp.endpoint_policy import EndpointPolicy
from src.integrations.mcp.gateway import MCPGateway
from src.integrations.mcp.gateway_models import MCPCancelStatus
from src.integrations.mcp.protocol import (
    MCP_PROTOCOL_VERSION_2025_11_25,
    MCP_PROTOCOL_VERSION_2026_07_28,
    MCPNegotiatedSession,
    MCPTransportResponse,
)
from src.integrations.mcp.recovery_worker import MCPRemoteTaskRecoveryWorker
from src.integrations.mcp.temporary_results import (
    MCPTemporaryResultCapacity,
    MCPTemporaryResultCapacityConfig,
    MCPTemporaryResultStore,
)
from src.integrations.mcp.user_client import UserMCPClientFactory
from tests.master_key_support import recovery_cipher
from src.storage.sqlite import (
    SQLiteStorage,
    bootstrap_sqlite_database,
    create_sqlite_engine,
    create_sqlite_session_factory,
)


class _ExecutionClient:
    def __init__(self, result: Mapping[str, Any]) -> None:
        self.result = dict(result)
        self.calls: list[tuple[str, Mapping[str, Any], Mapping[str, Any]]] = []
        self.closed = False
        self.server_capabilities = {
            "tools": {},
            "tasks": {"requests": {"tools.call": {}}},
        }
        self.negotiated_session = MCPNegotiatedSession(
            server_id="server-a",
            requested_protocol_version=MCP_PROTOCOL_VERSION_2025_11_25,
            negotiated_protocol_version=MCP_PROTOCOL_VERSION_2025_11_25,
            transport_family="streamable_http",
            server_capabilities=self.server_capabilities,
            server_info={"name": "fake"},
            pinned_protocol_version=True,
        )

    async def initialize(self) -> Mapping[str, Any]:
        return {"protocolVersion": MCP_PROTOCOL_VERSION_2025_11_25}

    async def list_tools(self) -> list[Mapping[str, Any]]:
        return [
            {
                "name": "lookup",
                "inputSchema": {"type": "object"},
                "execution": {"taskSupport": "required"},
            }
        ]

    async def call_tool(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
        **kwargs: Any,
    ) -> Mapping[str, Any]:
        self.calls.append((tool_name, dict(arguments), dict(kwargs)))
        return dict(self.result)

    async def close(self) -> None:
        self.closed = True


class _TaskTransport:
    def __init__(self, results: list[Mapping[str, Any]]) -> None:
        self._results = [dict(result) for result in results]
        self.requests: list[dict[str, Any]] = []
        self.closed = False

    async def send(
        self,
        message: Mapping[str, Any],
        *,
        protocol_version: str,
        session_id: str | None = None,
        timeout_seconds: float | None = None,
        last_event_id: str | None = None,
    ) -> MCPTransportResponse:
        self.requests.append(
            {
                "message": dict(message),
                "protocol_version": protocol_version,
                "session_id": session_id,
                "timeout_seconds": timeout_seconds,
                "last_event_id": last_event_id,
            }
        )
        result = self._results.pop(0)
        return MCPTransportResponse(
            message={
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": result,
            }
        )

    async def close(self) -> None:
        self.closed = True


class _Resolver:
    def resolve(self, _hostname: str, _port: int) -> tuple[str, ...]:
        return ("8.8.8.8",)


class MCP20251125TaskRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.db_path = Path(self._temporary.name) / "mcp-2025-task.sqlite3"
        self.engine = create_sqlite_engine(self.db_path)
        bootstrap_sqlite_database(self.engine)
        self.storage = SQLiteStorage(create_sqlite_session_factory(self.engine))
        self.now = datetime(2026, 8, 13, 12, 0, 0)
        self.context = MCPRecoveryCallContext(
            owner_user_id="alice",
            task_id="task-a",
            node_id="node-a",
            call_ref="call-a",
        )
        self.recovery = MCPRecoveryService(
            self.storage,
            recovery_cipher(b"2" * 32),
            now_fn=lambda: self.now,
        )
        await self.storage.save_conversation(Conversation("conv-a", "alice"))
        await self.storage.save_task(Task("task-a", "conv-a", "message-a"))
        await self.storage.save_mcp_branch_record(
            MCPBranchRecord(
                branch_id="branch-a",
                owner_user_id="alice",
                task_id="task-a",
                node_id="node-a",
                status="ready",
                created_at=self.now,
                updated_at=self.now,
            )
        )
        self.assertTrue(
            await self.storage.reserve_mcp_call(
                MCPCallRecord(
                    call_ref="call-a",
                    branch_id="branch-a",
                    owner_user_id="alice",
                    task_id="task-a",
                    node_id="node-a",
                    server_id="server-a",
                    tool_name="lookup",
                    status="reserved",
                    call_sequence=1,
                    arguments_sha256="args-sha",
                    server_security_version=1,
                    input_schema_sha256="schema-sha",
                    protocol_version=MCP_PROTOCOL_VERSION_2025_11_25,
                    may_have_dispatched=True,
                    created_at=self.now,
                    updated_at=self.now,
                )
            )
        )

    async def asyncTearDown(self) -> None:
        self.engine.dispose()
        self._temporary.cleanup()

    async def _create_binding(self) -> MCP2025TaskCreatedOutcome:
        execution = _ExecutionClient(
            {
                "taskId": "raw-2025-task-id",
                "status": {"state": "working", "message": "queued"},
                "pollInterval": 0,
                "_meta": {
                    "io.modelcontextprotocol/related-task": {
                        "taskId": "raw-2025-task-id"
                    }
                },
            }
        )
        adapter = MCP2025TasksAdapter(
            execution,
            server_id="server-a",
            recovery_service=self.recovery,
            safe_ref_factory=lambda prefix: f"{prefix}:safe-2025",
        )
        await adapter.initialize()
        await adapter.list_tools()
        outcome = await adapter.call_tool(
            "lookup",
            {"query": "rice"},
            recovery_context=self.context,
            result_sink=object(),
            input_responses=None,
            sealed_request_state_ref=None,
        )
        self.assertIsInstance(outcome, MCP2025TaskCreatedOutcome)
        _, _, call_kwargs = execution.calls[0]
        self.assertTrue(call_kwargs["task_augmented"])
        self.assertEqual(call_kwargs["task_ttl_ms"], 60_000)
        self.assertNotIn("result_sink", call_kwargs)
        self.assertNotIn("input_responses", call_kwargs)
        self.assertNotIn("sealed_request_state_ref", call_kwargs)
        self.assertNotIn("raw-2025-task-id", repr(outcome))
        binding = await self.storage.get_mcp_remote_task_binding(
            "alice", "task-a", outcome.safe_remote_task_ref
        )
        self.assertIsNotNone(binding)
        assert binding is not None
        self.assertEqual(binding.protocol_version, MCP_PROTOCOL_VERSION_2025_11_25)
        self.assertNotIn(b"raw-2025-task-id", binding.remote_task_ciphertext)
        self.assertNotIn("raw-2025-task-id", repr(binding))
        await self.storage.save_task_node(
            TaskNode(
                node_id=self.context.node_id,
                task_id=self.context.task_id,
                capability_id="mcp.dispatch",
                status=NodeStatus.WAITING_FOR_DEPENDENCY,
            )
        )
        await self.storage.publish_mcp_remote_task_binding(
            self.context.owner_user_id,
            self.context.task_id,
            outcome.safe_remote_task_ref,
            published_at=self.now,
        )
        return outcome

    async def test_restart_polls_then_fetches_result_without_replaying_call(self) -> None:
        created = await self._create_binding()
        working_transport = _TaskTransport(
            [
                {
                    "taskId": "raw-2025-task-id",
                    "status": {"state": "working"},
                    "pollInterval": 1000,
                }
            ]
        )
        first = MCPRemoteTaskRecoveryWorker(
            storage=self.storage,
            client_factory=lambda _binding: MCP2025TaskRecoveryClient(
                server_id="server-a",
                transport=working_transport,
                recovery_service=self.recovery,
            ),
            instance_id="worker-2025-first",
            now_fn=lambda: self.now,
        )
        self.assertEqual(await first.run_once(), 1)

        self.engine.dispose()
        self.engine = create_sqlite_engine(self.db_path)
        bootstrap_sqlite_database(self.engine)
        self.storage = SQLiteStorage(create_sqlite_session_factory(self.engine))
        self.recovery = MCPRecoveryService(
            self.storage,
            recovery_cipher(b"2" * 32),
            now_fn=lambda: self.now,
        )
        self.now += timedelta(seconds=1)
        completed_transport = _TaskTransport(
            [
                {
                    "taskId": "raw-2025-task-id",
                    "status": {"state": "completed", "message": "done"},
                },
                {
                    "content": [{"type": "text", "text": "done"}],
                    "structuredContent": {"ok": True},
                    "isError": False,
                    "_meta": {
                        "io.modelcontextprotocol/related-task": {
                            "taskId": "raw-2025-task-id"
                        }
                    },
                },
            ]
        )
        persisted: list[Mapping[str, Any]] = []

        async def persist_result(_binding, result: Mapping[str, Any]) -> str:
            persisted.append(dict(result))
            return "mcp-result-safe-2025"

        second = MCPRemoteTaskRecoveryWorker(
            storage=self.storage,
            client_factory=lambda _binding: MCP2025TaskRecoveryClient(
                server_id="server-a",
                transport=completed_transport,
                recovery_service=self.recovery,
            ),
            instance_id="worker-2025-second",
            result_persister=persist_result,
            now_fn=lambda: self.now,
        )
        self.assertEqual(await second.run_once(), 1)

        methods = [
            request["message"]["method"]
            for request in working_transport.requests + completed_transport.requests
        ]
        self.assertEqual(methods, ["tasks/get", "tasks/get", "tasks/result"])
        self.assertNotIn("initialize", methods)
        self.assertNotIn("tools/list", methods)
        self.assertNotIn("tools/call", methods)
        for request in working_transport.requests + completed_transport.requests:
            self.assertEqual(
                request["protocol_version"], MCP_PROTOCOL_VERSION_2025_11_25
            )
            self.assertIsNone(request["session_id"])
        self.assertEqual(persisted[0]["structuredContent"], {"ok": True})
        self.assertNotIn("_meta", persisted[0])
        self.assertNotIn("raw-2025-task-id", repr(persisted))
        binding = await self.storage.get_mcp_remote_task_binding(
            "alice", "task-a", created.safe_remote_task_ref
        )
        self.assertIsNotNone(binding)
        assert binding is not None
        self.assertEqual(binding.last_status, "completed")
        call = await self.storage.get_mcp_call_record("alice", "task-a", "call-a")
        self.assertIsNotNone(call)
        assert call is not None
        self.assertEqual(call.status, "completed")
        self.assertEqual(call.result_ref, "mcp-result-safe-2025")

    async def test_immediate_terminal_create_task_is_queried_once_after_restart(self) -> None:
        expected = {
            "completed": ("completed", None, True),
            "failed": ("failed", "mcp_remote_task_failed", False),
            "cancelled": ("cancelled", "mcp_remote_task_cancelled", False),
        }
        execution_clients: list[_ExecutionClient] = []
        safe_refs: dict[str, str] = {}
        for status, (call_status, error_code, has_result) in expected.items():
            task_id = f"task-immediate-{status}"
            node_id = f"node-immediate-{status}"
            call_ref = f"call-immediate-{status}"
            branch_id = f"branch-immediate-{status}"
            await self.storage.save_task(Task(task_id, "conv-a", f"message-{status}"))
            await self.storage.save_mcp_branch_record(
                MCPBranchRecord(
                    branch_id=branch_id,
                    owner_user_id="alice",
                    task_id=task_id,
                    node_id=node_id,
                    status="ready",
                    created_at=self.now,
                    updated_at=self.now,
                )
            )
            self.assertTrue(
                await self.storage.reserve_mcp_call(
                    MCPCallRecord(
                        call_ref=call_ref,
                        branch_id=branch_id,
                        owner_user_id="alice",
                        task_id=task_id,
                        node_id=node_id,
                        server_id="server-a",
                        tool_name="lookup",
                        status="active",
                        call_sequence=1,
                        arguments_sha256="args-sha",
                        server_security_version=1,
                        input_schema_sha256="schema-sha",
                        protocol_version=MCP_PROTOCOL_VERSION_2025_11_25,
                        may_have_dispatched=True,
                        created_at=self.now,
                        updated_at=self.now,
                    )
                )
            )
            raw_task_id = f"raw-immediate-{status}"
            execution = _ExecutionClient(
                {
                    "taskId": raw_task_id,
                    "status": {"state": status},
                    "_meta": {
                        "io.modelcontextprotocol/related-task": {
                            "taskId": raw_task_id
                        }
                    },
                }
            )
            execution_clients.append(execution)
            adapter = MCP2025TasksAdapter(
                execution,
                server_id="server-a",
                recovery_service=self.recovery,
                safe_ref_factory=lambda prefix, status=status: (
                    f"{prefix}:immediate-{status}"
                ),
            )
            await adapter.initialize()
            await adapter.list_tools()
            outcome = await adapter.call_tool(
                "lookup",
                {"query": "rice"},
                recovery_context=MCPRecoveryCallContext(
                    "alice", task_id, node_id, call_ref
                ),
            )

            self.assertEqual(len(execution.calls), 1)
            binding = await self.storage.get_mcp_remote_task_binding(
                "alice", task_id, outcome.safe_remote_task_ref
            )
            self.assertIsNotNone(binding)
            assert binding is not None
            self.assertEqual(binding.last_status, status)
            self.assertIsNone(binding.terminal_at)
            self.assertIsNone(binding.next_poll_at)
            await self.storage.save_task_node(
                TaskNode(
                    node_id=node_id,
                    task_id=task_id,
                    capability_id="mcp.dispatch",
                    status=NodeStatus.WAITING_FOR_DEPENDENCY,
                )
            )
            await self.storage.publish_mcp_remote_task_binding(
                "alice", task_id, outcome.safe_remote_task_ref, published_at=self.now
            )
            call = await self.storage.get_mcp_call_record(
                "alice", task_id, call_ref
            )
            self.assertIsNotNone(call)
            assert call is not None
            self.assertEqual(call.status, "active")
            self.assertIsNone(call.result_ref)
            self.assertIsNone(call.terminal_at)
            safe_refs[status] = outcome.safe_remote_task_ref

        self.engine.dispose()
        self.engine = create_sqlite_engine(self.db_path)
        bootstrap_sqlite_database(self.engine)
        self.storage = SQLiteStorage(create_sqlite_session_factory(self.engine))
        self.recovery = MCPRecoveryService(
            self.storage,
            recovery_cipher(b"2" * 32),
            now_fn=lambda: self.now + timedelta(seconds=1),
        )
        transports: dict[str, _TaskTransport] = {}
        for status, safe_ref in safe_refs.items():
            raw_task_id = f"raw-immediate-{status}"
            responses: list[Mapping[str, Any]] = [
                {"taskId": raw_task_id, "status": {"state": status}}
            ]
            if status in {"completed", "failed"}:
                responses.append(
                    {
                        "content": [{"type": "text", "text": "final-2025"}],
                        "structuredContent": {"status": "complete"},
                        "isError": False,
                        "_meta": {
                            "io.modelcontextprotocol/related-task": {
                                "taskId": raw_task_id
                            }
                        },
                    }
                )
            transports[safe_ref] = _TaskTransport(responses)

        persisted_results: dict[str, Mapping[str, Any]] = {}

        async def persist_result(binding, result: Mapping[str, Any]) -> str:
            persisted_results[binding.safe_remote_task_ref] = dict(result)
            return f"result:{binding.safe_remote_task_ref}"

        worker = MCPRemoteTaskRecoveryWorker(
            storage=self.storage,
            client_factory=lambda binding: MCP2025TaskRecoveryClient(
                server_id="server-a",
                transport=transports[binding.safe_remote_task_ref],
                recovery_service=self.recovery,
            ),
            instance_id="worker-immediate-terminal-restart",
            result_persister=persist_result,
            now_fn=lambda: self.now + timedelta(seconds=1),
        )
        self.assertEqual(await worker.run_once(), 3)
        self.assertTrue(all(len(client.calls) == 1 for client in execution_clients))
        for status, (call_status, error_code, has_result) in expected.items():
            methods = [
                request["message"]["method"]
                for request in transports[safe_refs[status]].requests
            ]
            self.assertEqual(
                methods,
                ["tasks/get", "tasks/result"]
                if status == "completed"
                else ["tasks/get"],
            )
            self.assertNotIn("tools/call", methods)
            call = await self.storage.get_mcp_call_record(
                "alice", f"task-immediate-{status}", f"call-immediate-{status}"
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

    async def test_unknown_2025_create_task_status_fails_closed_after_one_call(self) -> None:
        execution = _ExecutionClient(
            {
                "taskId": "raw-unknown-status",
                "status": {"state": "unknown"},
                "_meta": {
                    "io.modelcontextprotocol/related-task": {
                        "taskId": "raw-unknown-status"
                    }
                },
            }
        )
        adapter = MCP2025TasksAdapter(
            execution,
            server_id="server-a",
            recovery_service=self.recovery,
        )
        await adapter.initialize()
        await adapter.list_tools()

        with self.assertRaisesRegex(MCPProtocolError, "Task status is invalid"):
            await adapter.call_tool(
                "lookup",
                {"query": "rice"},
                recovery_context=self.context,
            )
        self.assertEqual(len(execution.calls), 1)
        self.assertEqual(
            await self.storage.list_due_mcp_remote_task_bindings(now=self.now),
            [],
        )

    async def test_gateway_cancel_recovers_2025_task_without_replaying_call(self) -> None:
        await self._create_binding()
        transport = _TaskTransport([{"cancelled": True}])
        worker = MCPRemoteTaskRecoveryWorker(
            storage=self.storage,
            client_factory=lambda _binding: MCP2025TaskRecoveryClient(
                server_id="server-a",
                transport=transport,
                recovery_service=self.recovery,
            ),
            instance_id="worker-2025-cancel",
            now_fn=lambda: self.now,
        )
        result_root = Path(self._temporary.name) / "cancel-results"
        gateway = MCPGateway(
            storage=self.storage,
            gateway_instance_id="gateway-2025-cancel",
            credential_loader=lambda _server: {},
            client_factory=lambda _server, _credentials, _endpoint: object(),
            endpoint_revalidator=lambda server: server.endpoint_url,
            result_store=MCPTemporaryResultStore(
                result_root, memory_threshold_bytes=1024
            ),
            capacity=MCPTemporaryResultCapacity(
                MCPTemporaryResultCapacityConfig(1, 1),
                storage_root=result_root,
                free_bytes=lambda _path: 1024 * 1024,
            ),
            remote_task_canceller=worker.cancel_remote_task,
        )
        outcome = await gateway.cancel_call_for_task(
            "task-a", "call-a", "user_requested"
        )

        self.assertEqual(outcome.status, MCPCancelStatus.CANCELLED)
        self.assertTrue(outcome.remote_stop_confirmed)
        self.assertEqual(
            [request["message"]["method"] for request in transport.requests],
            ["tasks/cancel"],
        )
        self.assertEqual(
            transport.requests[0]["message"]["params"],
            {"taskId": "raw-2025-task-id", "reason": "user_requested"},
        )
        self.assertNotIn("tools/call", repr(transport.requests))
        await gateway.aclose()

    async def test_cross_version_binding_is_rejected_before_network(self) -> None:
        await self.recovery.save_remote_task(
            self.context,
            server_id="server-a",
            protocol_version=MCP_PROTOCOL_VERSION_2026_07_28,
            safe_remote_task_ref="mcp-task:safe-2026",
            remote_task_id="raw-2026-task-id",
            status="working",
            poll_interval_ms=1000,
        )
        transport = _TaskTransport([])
        client = MCP2025TaskRecoveryClient(
            server_id="server-a",
            transport=transport,
            recovery_service=self.recovery,
        )

        with self.assertRaisesRegex(MCPProtocolError, "Unknown or expired"):
            await client.tasks_get(
                "mcp-task:safe-2026",
                recovery_context=self.context,
            )

        self.assertEqual(transport.requests, [])

    async def test_factory_uses_durable_version_for_auto_and_rejects_pin_mismatch(
        self,
    ) -> None:
        factory = UserMCPClientFactory(
            EndpointPolicy(resolver=_Resolver()),
            recovery_service=self.recovery,
        )
        auto = UserMCPServer(
            server_id="server-a",
            owner_user_id="alice",
            display_name="Server",
            routing_description="",
            endpoint_url="https://example.test/mcp",
            transport=UserMCPTransport.STREAMABLE_HTTP,
            protocol_preference=UserMCPProtocolPreference.AUTO,
        )
        pinned_2025 = UserMCPServer(
            server_id="server-a",
            owner_user_id="alice",
            display_name="Server",
            routing_description="",
            endpoint_url="https://example.test/mcp",
            transport=UserMCPTransport.STREAMABLE_HTTP,
            protocol_preference=UserMCPProtocolPreference.V2025_11_25,
        )
        pinned_2026 = UserMCPServer(
            server_id="server-a",
            owner_user_id="alice",
            display_name="Server",
            routing_description="",
            endpoint_url="https://example.test/mcp",
            transport=UserMCPTransport.STREAMABLE_HTTP,
            protocol_preference=UserMCPProtocolPreference.V2026_07_28,
        )
        endpoint = await factory.revalidate_endpoint(auto)

        auto_client = await factory.create_task_recovery(
            auto, {}, endpoint, protocol_version=MCP_PROTOCOL_VERSION_2025_11_25
        )
        pinned_client = await factory.create_task_recovery(
            pinned_2025,
            {},
            endpoint,
            protocol_version=MCP_PROTOCOL_VERSION_2025_11_25,
        )
        self.assertIsInstance(auto_client, MCP2025TaskRecoveryClient)
        self.assertIsInstance(pinned_client, MCP2025TaskRecoveryClient)
        await auto_client.close()
        await pinned_client.close()
        with self.assertRaisesRegex(
            MCPProtocolError, "mcp_remote_task_protocol_binding_mismatch"
        ):
            await factory.create_task_recovery(
                pinned_2026,
                {},
                endpoint,
                protocol_version=MCP_PROTOCOL_VERSION_2025_11_25,
            )


if __name__ == "__main__":
    unittest.main()
