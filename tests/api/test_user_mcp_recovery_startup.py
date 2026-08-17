from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from src.api.dto import SubmitMessageRequest
from src.api.runtime import ApiRuntime, build_api_runtime
from src.core.enums import (
    NodeStatus,
    TaskStatus,
    UserMCPAuthType,
    UserMCPHealthStatus,
    UserMCPProtocolPreference,
    UserMCPTransport,
)
from src.core.models import (
    Conversation,
    MCPBranchRecord,
    MCPCallRecord,
    MCPRemoteTaskBinding,
    Task,
    TaskNode,
    UserMCPCredentialRecord,
    UserMCPServer,
)
from src.integrations.mcp.recovery_worker import MCPRemoteTaskTerminalMetricSample
from src.integrations.mcp.adapter_2025_tasks import (
    MCP2025TaskResult,
    MCP2025TaskState,
)
from src.integrations.mcp.credentials import (
    MCPRecoveryCallContext,
    MCPRecoveryService,
)
from src.integrations.mcp.endpoint_policy import EndpointPolicyError
from src.integrations.mcp.rollout_evidence import (
    MCPMetricErrorCategory,
    MCPMetricExecutionPath,
    MCPMetricName,
    MCPMetricResultCategory,
    MCPMetricRoutingMode,
    MCPSafetyRedLine,
)
from src.integrations.mcp.temporary_results import MCPTemporaryResultRef
from tests.api.support import InMemoryTaskRuntimeSidecar
from tests.master_key_support import recovery_cipher


class UserMCPRecoveryStartupTest(unittest.IsolatedAsyncioTestCase):
    async def test_task_assignment_records_one_legacy_route_metric(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(
                os.environ,
                {
                    "MAF_STATE_STORE_BACKEND": "sqlite",
                    "MAF_USER_MCP_MAX_ACTIVE_CALLS": "2",
                    "MAF_USER_MCP_TEMPORARY_DISK_LOW_WATERMARK_BYTES": "1",
                },
                clear=False,
            ),
        ):
            runtime = self._build_runtime(Path(directory))
            recorder = AsyncMock()
            runtime._mcp_rollout_metric_recorder = recorder

            await runtime.submit_message(
                "conv-route-metric",
                SubmitMessageRequest(
                    conversation_id="conv-route-metric",
                    content="hello",
                ),
                authenticated_username="alice",
            )

            self.assertEqual(recorder.record_count.await_count, 1)
            args, kwargs = recorder.record_count.await_args
            self.assertEqual(args, (MCPMetricName.ROUTE_REQUESTS_TOTAL,))
            self.assertEqual(
                kwargs["labels"].execution_path,
                MCPMetricExecutionPath.LEGACY,
            )
            self.assertEqual(
                kwargs["labels"].routing_mode,
                MCPMetricRoutingMode.OFF,
            )
            await runtime.shutdown()

    def test_rollout_routing_metadata_is_system_managed(self) -> None:
        metadata = {
            "mcp_execution_mode": "forged",
            "mcp_shadow_enabled": True,
            "mcp_rollout_config_version": 999,
            "mcp_route_reason_code": "forged",
            "user_key": "retained",
        }

        self.assertEqual(
            ApiRuntime._drop_user_supplied_system_metadata(metadata),
            {"user_key": "retained"},
        )

    def _build_runtime(self, root: Path, *, planner_text_generator=None):
        key_path = root / "mcp.key"
        key_path.write_text(
            "YWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWE=",
            encoding="ascii",
        )
        key_path.chmod(0o600)
        return build_api_runtime(
            database_path=root / "runtime.sqlite3",
            audit_log_path=root / "audit.jsonl",
            enable_user_mcp=True,
            master_key_bytes=b"a" * 32,
            enable_platform_llm=False,
            enable_llm_planner=False,
            planner_text_generator=planner_text_generator,
            enable_conversation_title_llm=False,
            enable_conversation_memory=False,
            runtime_sidecar_client=InMemoryTaskRuntimeSidecar(),
        )

    async def test_runtime_restart_completes_2025_task_result_into_safe_store(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(
                os.environ,
                {
                    "MAF_STATE_STORE_BACKEND": "sqlite",
                    "MAF_USER_MCP_MAX_ACTIVE_CALLS": "2",
                    "MAF_USER_MCP_TEMPORARY_DISK_LOW_WATERMARK_BYTES": "1",
                },
                clear=False,
            ),
        ):
            runtime = self._build_runtime(Path(directory))
            now = runtime._utcnow_naive()
            server = UserMCPServer(
                server_id="server-2025",
                owner_user_id="alice",
                display_name="Private 2025 server",
                routing_description="private",
                endpoint_url="https://example.test/mcp",
                transport=UserMCPTransport.STREAMABLE_HTTP,
                protocol_preference=UserMCPProtocolPreference.V2025_11_25,
                health_status=UserMCPHealthStatus.AVAILABLE,
                created_at=now,
                updated_at=now,
            )
            await runtime.storage.save_conversation(Conversation("conv-2025", "alice"))
            await runtime.storage.save_task(
                Task(
                    "task-2025",
                    "conv-2025",
                    "message-2025",
                    status=TaskStatus.RUNNING,
                )
            )
            await runtime.storage.create_user_mcp_server(server)
            await runtime.storage.save_mcp_branch_record(
                MCPBranchRecord(
                    branch_id="branch-2025",
                    owner_user_id="alice",
                    task_id="task-2025",
                    node_id="node-2025",
                    status="running",
                    created_at=now,
                    updated_at=now,
                )
            )
            call = MCPCallRecord(
                call_ref="call-2025",
                branch_id="branch-2025",
                owner_user_id="alice",
                task_id="task-2025",
                node_id="node-2025",
                server_id=server.server_id,
                tool_name="lookup",
                status="active",
                call_sequence=1,
                arguments_sha256="arguments-hash",
                server_security_version=server.security_version,
                input_schema_sha256="schema-hash",
                protocol_version="2025-11-25",
                may_have_dispatched=True,
                created_at=now,
                updated_at=now,
            )
            self.assertTrue(await runtime.storage.reserve_mcp_call(call))
            context = MCPRecoveryCallContext(
                "alice", "task-2025", "node-2025", "call-2025"
            )
            recovery = MCPRecoveryService(
                runtime.storage,
                recovery_cipher(b"a" * 32),
                now_fn=lambda: now,
            )
            await recovery.save_remote_task(
                context,
                server_id=server.server_id,
                protocol_version="2025-11-25",
                safe_remote_task_ref="safe-remote-2025",
                remote_task_id="raw-private-task-id",
                status="working",
                poll_interval_ms=0,
            )
            await runtime.storage.save_task_node(
                TaskNode(
                    node_id="node-2025",
                    task_id="task-2025",
                    capability_id="mcp.dispatch",
                    status=NodeStatus.WAITING_FOR_DEPENDENCY,
                )
            )
            await runtime.storage.publish_mcp_remote_task_binding(
                "alice",
                "task-2025",
                "safe-remote-2025",
                published_at=now,
                continuation_plan={
                    "task_id": "task-2025",
                    "nodes": [
                        {
                            "node_id": "node-2025",
                            "capability_id": "mcp.dispatch",
                            "input_payload": {},
                            "metadata": {},
                            "depends_on": [],
                            "criticality": "required",
                            "retry_policy": {},
                            "timeout_policy": {},
                            "resource_class": None,
                        }
                    ],
                },
            )

            class _Client:
                async def tasks_get(self, safe_ref, *, recovery_context):
                    self.assert_safe(safe_ref, recovery_context)
                    return MCP2025TaskState(safe_ref, "completed", True)

                async def tasks_result(self, safe_ref, *, recovery_context):
                    self.assert_safe(safe_ref, recovery_context)
                    return MCP2025TaskResult(
                        safe_ref,
                        {
                            "content": [{"type": "text", "text": "done"}],
                            "structuredContent": {"ok": True},
                            "isError": False,
                        },
                    )

                def assert_safe(self, safe_ref, recovery_context):
                    if safe_ref != "safe-remote-2025" or recovery_context != context:
                        raise AssertionError("unsafe recovery context")

                async def close(self):
                    return None

            runtime.mcp_remote_task_recovery_worker._client_factory = (
                lambda _binding: _Client()
            )

            self.assertEqual(
                await runtime.mcp_remote_task_recovery_worker.run_once(), 1
            )
            finished = await runtime.storage.get_mcp_call_record(
                "alice", "task-2025", "call-2025"
            )
            self.assertIsNotNone(finished)
            self.assertEqual(finished.status, "completed")
            self.assertRegex(finished.result_ref or "", r"^mcp-result-")
            self.assertNotEqual(finished.result_ref, "safe-remote-2025")
            self.assertNotIn("raw-private-task-id", repr(finished))
            payload = b"".join(
                [
                    chunk
                    async for chunk in runtime.user_mcp_result_store.iter_bytes(
                        MCPTemporaryResultRef(
                            ref=finished.result_ref or "",
                            size_bytes=0,
                            sha256="",
                            storage="memory",
                        )
                    )
                ]
            )
            self.assertEqual(json.loads(payload), {
                "content": [{"type": "text", "text": "done"}],
                "structuredContent": {"ok": True},
                "isError": False,
            })
            scheduled: list[str] = []

            async def completed_execution() -> None:
                return None

            async def capture_execution(request):
                scheduled.append(request.metadata["mcp_remote_task_continuation_id"])
                return asyncio.create_task(completed_execution())

            runtime._schedule_execution = capture_execution
            self.assertEqual(await runtime._run_mcp_continuation_commands_once(), 1)
            self.assertEqual(scheduled, ["mcp-remote-terminal:call-2025"])
            self.assertEqual(await runtime._run_mcp_continuation_commands_once(), 0)
            self.assertEqual(len(scheduled), 1)
            await runtime.shutdown()

    async def test_runtime_starts_and_stops_continuous_zero_series_producer(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(
                os.environ,
                {
                    "MAF_STATE_STORE_BACKEND": "sqlite",
                    "MAF_USER_MCP_MAX_ACTIVE_CALLS": "2",
                    "MAF_USER_MCP_TEMPORARY_DISK_LOW_WATERMARK_BYTES": "1",
                    "MCP_USER_SCOPED_GATEWAY_ENABLED": "true",
                    "MCP_ROUTING_MODE": "enforce",
                    "MCP_LEGACY_GLOBAL_RUNTIME_ENABLED": "true",
                    "MCP_ENFORCE_PERCENT": "100",
                    "MCP_ENFORCE_HASH_SALT": "test-only-rollout-salt",
                    "MCP_ROLLOUT_ENVIRONMENT_ID": "test-environment",
                    "MCP_ROLLOUT_DEPLOYMENT_ID": "test-deployment",
                    "MCP_ROLLOUT_STAGE": "full_enforce",
                    "MCP_ROLLOUT_ACTIVATION_ID": "test-activation",
                    "MCP_ROLLOUT_INSTANCE_ID": "test-instance",
                },
                clear=False,
            ),
        ):
            async def planner_text_generator(_prompt: str) -> str:
                return "{}"

            runtime = self._build_runtime(
                Path(directory),
                planner_text_generator=planner_text_generator,
            )
            self.assertIsNotNone(runtime._mcp_rollout_metric_recorder)
            lease_expiry_sink = (
                runtime.user_mcp_presence_service._lease_expired_observer
            )
            self.assertIsNotNone(lease_expiry_sink)
            metric_minute = datetime.now(timezone.utc).replace(
                second=0,
                microsecond=0,
            )
            await lease_expiry_sink()
            buckets = await runtime.storage.list_mcp_rollout_metric_buckets(
                "test-environment",
                "test-deployment",
                "full_enforce",
                window_started_at=metric_minute,
                window_ended_at=metric_minute + timedelta(minutes=1),
            )
            self.assertEqual(
                [
                    bucket.value
                    for bucket in buckets
                    if bucket.metric_name
                    == MCPMetricName.DISCONNECT_LEASE_EXPIRED_TOTAL.value
                ],
                [1],
            )
            zero_records = await runtime._mcp_rollout_metric_recorder.record_required_zero_series(
                bucket_started_at=metric_minute,
                bucket_ended_at=metric_minute + timedelta(minutes=1),
            )
            self.assertEqual(
                {
                    record.red_line
                    for record in zero_records
                    if record.red_line is not None
                },
                {red_line.value for red_line in MCPSafetyRedLine},
            )
            started = asyncio.Event()

            async def producer() -> None:
                started.set()
                await asyncio.Event().wait()

            runtime._mcp_rollout_metric_recorder.run_continuous_zero_series = producer
            runtime._admit_mcp_rollout_instance = AsyncMock()

            await runtime.start()
            producer_task = runtime._mcp_rollout_zero_series_task
            self.assertIsNotNone(producer_task)
            await asyncio.wait_for(started.wait(), timeout=1)

            await runtime.shutdown()

            self.assertTrue(producer_task.done())
            self.assertIsNone(runtime._mcp_rollout_zero_series_task)

    async def test_startup_converges_ordinary_call_before_starting_recovery_worker(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(
                os.environ,
                {
                    "MAF_STATE_STORE_BACKEND": "sqlite",
                    "MAF_USER_MCP_MAX_ACTIVE_CALLS": "2",
                    "MAF_USER_MCP_TEMPORARY_DISK_LOW_WATERMARK_BYTES": "1",
                },
                clear=False,
            ),
        ):
            runtime = self._build_runtime(Path(directory))
            now = datetime(2026, 8, 13, 12, 0, 0)
            await runtime.storage.save_conversation(Conversation("conv-a", "alice"))
            await runtime.storage.save_task(Task("task-a", "conv-a", "message-a"))
            await runtime.storage.save_mcp_branch_record(
                MCPBranchRecord(
                    branch_id="branch-a",
                    owner_user_id="alice",
                    task_id="task-a",
                    node_id="node-a",
                    status="running",
                    created_at=now,
                    updated_at=now,
                )
            )
            self.assertTrue(
                await runtime.storage.reserve_mcp_call(
                    MCPCallRecord(
                        call_ref="safe-call-a",
                        branch_id="branch-a",
                        owner_user_id="alice",
                        task_id="task-a",
                        node_id="node-a",
                        server_id="raw-server-id",
                        tool_name="private-tool-name",
                        status="active",
                        call_sequence=1,
                        arguments_sha256="arguments-hash",
                        server_security_version=1,
                        input_schema_sha256="schema-hash",
                        may_have_dispatched=True,
                        created_at=now,
                        updated_at=now,
                    )
                )
            )

            await runtime.start()
            try:
                recovered = await runtime.storage.get_mcp_call_record(
                    "alice", "task-a", "safe-call-a"
                )
                self.assertIsNotNone(recovered)
                self.assertEqual(recovered.status, "unknown")
                self.assertEqual(recovered.safe_error_code, "execution_status_unknown")
                self.assertIsNotNone(recovered.terminal_at)

                events = await runtime.storage.list_events_for_task("task-a")
                recovery_events = [
                    event
                    for event in events
                    if event.event_type == "mcp.execution_status_unknown"
                ]
                self.assertEqual(len(recovery_events), 1)
                self.assertEqual(
                    recovery_events[0].payload,
                    {
                        "safe_call_ref": runtime._mcp_audit_reference_signer.safe_reference(
                            "safe-call-a",
                            context="mcp-call-reference-v1",
                        ),
                        "status": "unknown",
                        "error_code": "execution_status_unknown",
                    },
                )
                self.assertNotIn("raw-server-id", repr(recovery_events[0]))
                self.assertNotIn("private-tool-name", repr(recovery_events[0]))

                audit_events = await runtime.storage.list_mcp_audit_events("alice")
                recovery_audits = [
                    event
                    for event in audit_events
                    if event.event_type == "mcp.execution_status_unknown"
                ]
                self.assertEqual(len(recovery_audits), 1)
                self.assertEqual(
                    recovery_audits[0].safe_payload, recovery_events[0].payload
                )

                worker = runtime.mcp_remote_task_recovery_worker
                self.assertIsNotNone(worker)
                self.assertIsNotNone(worker._runner)
                self.assertIsNotNone(
                    runtime.user_mcp_gateway._client_factory.__self__._recovery_service
                )
            finally:
                worker = runtime.mcp_remote_task_recovery_worker
                await runtime.shutdown()
                self.assertIsNone(worker._runner)

    async def test_remote_worker_factory_loads_owner_scoped_server_and_credentials(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(
                os.environ,
                {
                    "MAF_STATE_STORE_BACKEND": "sqlite",
                    "MAF_USER_MCP_MAX_ACTIVE_CALLS": "2",
                    "MAF_USER_MCP_TEMPORARY_DISK_LOW_WATERMARK_BYTES": "1",
                },
                clear=False,
            ),
        ):
            runtime = self._build_runtime(Path(directory))
            now = datetime(2026, 8, 13, 12, 0, 0)
            server = UserMCPServer(
                server_id="server-a",
                owner_user_id="alice",
                display_name="Private server",
                routing_description="private",
                endpoint_url="https://example.test/mcp",
                transport=UserMCPTransport.STREAMABLE_HTTP,
                auth_type=UserMCPAuthType.BEARER,
                credential_configured=True,
                created_at=now,
                updated_at=now,
            )
            encrypted = runtime.mcp_credential_cipher.encrypt(
                owner_user_id="alice",
                server_id="server-a",
                auth_type=str(UserMCPAuthType.BEARER),
                values={"token": "raw-secret-token"},
            )
            await runtime.storage.create_user_mcp_server(
                server,
                UserMCPCredentialRecord(
                    owner_user_id="alice",
                    server_id="server-a",
                    credential_ciphertext=encrypted.ciphertext,
                    credential_nonce=encrypted.nonce,
                    encryption_version=encrypted.encryption_version,
                    credential_updated_at=now,
                ),
            )
            user_client_factory = runtime.user_mcp_gateway._client_factory.__self__
            client = object()
            validated_endpoint = object()
            user_client_factory.revalidate_endpoint = AsyncMock(
                return_value=validated_endpoint
            )
            user_client_factory.create_task_recovery = AsyncMock(return_value=client)
            binding = MCPRemoteTaskBinding(
                safe_remote_task_ref="safe-remote-a",
                owner_user_id="alice",
                task_id="task-a",
                node_id="node-a",
                call_ref="safe-call-a",
                server_id="server-a",
                protocol_version="2026-07-28",
                remote_task_ciphertext=b"private",
                remote_task_nonce=b"private",
                encryption_version=1,
                last_status="working",
                next_poll_at=now,
                created_at=now,
                updated_at=now,
            )
            call = MCPCallRecord(
                call_ref=binding.call_ref,
                branch_id="branch-a",
                owner_user_id=binding.owner_user_id,
                task_id=binding.task_id,
                node_id=binding.node_id,
                server_id=binding.server_id,
                tool_name="private-tool",
                status="active",
                call_sequence=1,
                arguments_sha256="arguments-hash",
                server_security_version=server.security_version,
                input_schema_sha256="schema-hash",
                protocol_version=binding.protocol_version,
                may_have_dispatched=True,
                created_at=now,
                updated_at=now,
            )

            async def get_call(owner_user_id, task_id, call_ref):
                if (owner_user_id, task_id, call_ref) == (
                    call.owner_user_id,
                    call.task_id,
                    call.call_ref,
                ):
                    return call
                return None

            runtime.storage.get_mcp_call_record = AsyncMock(side_effect=get_call)

            factory = runtime.mcp_remote_task_recovery_worker._client_factory
            self.assertIs(await factory(binding), client)
            user_client_factory.create_task_recovery.assert_awaited_once_with(
                server,
                {"Authorization": "Bearer raw-secret-token"},
                validated_endpoint,
                protocol_version="2026-07-28",
            )
            credential_resolver = runtime.user_mcp_gateway._credential_loader.__self__
            credential_resolver.request_headers_for = AsyncMock(
                return_value={"Authorization": "must-not-be-read"}
            )
            user_client_factory.revalidate_endpoint = AsyncMock(
                side_effect=EndpointPolicyError(
                    "mcp_endpoint_private_forbidden"
                )
            )
            with self.assertRaisesRegex(
                EndpointPolicyError,
                "mcp_endpoint_private_forbidden",
            ):
                await factory(binding)
            credential_resolver.request_headers_for.assert_not_awaited()
            with self.assertRaisesRegex(
                RuntimeError, "mcp_recovery_server_unavailable"
            ):
                await factory(replace(binding, owner_user_id="bob"))
            with self.assertRaisesRegex(
                RuntimeError, "mcp_recovery_server_security_version_changed"
            ):
                await factory(replace(binding, protocol_version="2025-11-25"))
            self.assertEqual(await runtime.storage.list_events_for_task("task-a"), [])
            await runtime.shutdown()

    async def test_restart_unknown_counter_is_recorded_exactly_once(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(
                os.environ,
                {
                    "MAF_STATE_STORE_BACKEND": "sqlite",
                    "MAF_USER_MCP_MAX_ACTIVE_CALLS": "2",
                    "MAF_USER_MCP_TEMPORARY_DISK_LOW_WATERMARK_BYTES": "1",
                    "MCP_USER_SCOPED_GATEWAY_ENABLED": "true",
                    "MCP_ROUTING_MODE": "enforce",
                    "MCP_LEGACY_GLOBAL_RUNTIME_ENABLED": "true",
                    "MCP_ENFORCE_PERCENT": "100",
                    "MCP_ENFORCE_HASH_SALT": "test-only-rollout-salt",
                    "MCP_ROLLOUT_ENVIRONMENT_ID": "test-environment",
                    "MCP_ROLLOUT_DEPLOYMENT_ID": "test-deployment",
                    "MCP_ROLLOUT_STAGE": "full_enforce",
                    "MCP_ROLLOUT_ACTIVATION_ID": "test-activation",
                    "MCP_ROLLOUT_INSTANCE_ID": "test-instance",
                },
                clear=False,
            ),
        ):
            async def planner_text_generator(_prompt: str) -> str:
                return "{}"

            runtime = self._build_runtime(
                Path(directory), planner_text_generator=planner_text_generator
            )
            now = datetime(2026, 8, 13, 12, 4, 30)
            await runtime.storage.save_conversation(Conversation("conv-a", "alice"))
            await runtime.storage.save_task(
                Task(
                    "task-a",
                    "conv-a",
                    "message-a",
                    mcp_execution_mode="user_scoped",
                    mcp_shadow_enabled=False,
                    mcp_rollout_config_version=runtime.mcp_rollout_config.config_version,
                    mcp_route_reason_code="enforce_selected",
                    mcp_rollout_mode="enforce",
                )
            )
            await runtime.storage.create_user_mcp_server(
                UserMCPServer(
                    server_id="server-a",
                    owner_user_id="alice",
                    display_name="Private server",
                    routing_description="private",
                    endpoint_url="https://example.test/mcp",
                    transport=UserMCPTransport.STREAMABLE_HTTP,
                    created_at=now,
                    updated_at=now,
                ),
                None,
            )
            await runtime.storage.save_mcp_branch_record(
                MCPBranchRecord(
                    branch_id="branch-a",
                    owner_user_id="alice",
                    task_id="task-a",
                    node_id="node-a",
                    status="running",
                    created_at=now,
                    updated_at=now,
                )
            )
            self.assertTrue(
                await runtime.storage.reserve_mcp_call(
                    MCPCallRecord(
                        call_ref="safe-call-a",
                        branch_id="branch-a",
                        owner_user_id="alice",
                        task_id="task-a",
                        node_id="node-a",
                        server_id="server-a",
                        tool_name="private-tool",
                        status="active",
                        call_sequence=1,
                        arguments_sha256="arguments-hash",
                        server_security_version=1,
                        input_schema_sha256="schema-hash",
                        protocol_version="2026-07-28",
                        may_have_dispatched=True,
                        created_at=now,
                        updated_at=now,
                    )
                )
            )

            await runtime._recover_user_mcp_calls()
            await runtime._recover_user_mcp_calls()

            recovered = await runtime.storage.get_mcp_call_record(
                "alice", "task-a", "safe-call-a"
            )
            self.assertIsNotNone(recovered)
            self.assertIsNotNone(recovered.terminal_at)
            terminal_minute = recovered.terminal_at.replace(
                tzinfo=timezone.utc, second=0, microsecond=0
            )

            buckets = await runtime.storage.list_mcp_rollout_metric_buckets(
                "test-environment",
                "test-deployment",
                "full_enforce",
                window_started_at=terminal_minute,
                window_ended_at=terminal_minute + timedelta(minutes=1),
            )
            unknown = [
                bucket
                for bucket in buckets
                if bucket.metric_name == MCPMetricName.TOOL_CALL_UNKNOWN_TOTAL.value
            ]
            self.assertEqual(len(unknown), 1)
            self.assertEqual(unknown[0].value, 1)
            self.assertEqual(unknown[0].call_kind, "ordinary")
            totals = [
                bucket
                for bucket in buckets
                if bucket.metric_name == MCPMetricName.TOOL_CALLS_TOTAL.value
                and bucket.result_category == MCPMetricResultCategory.UNKNOWN.value
            ]
            durations = [
                bucket
                for bucket in buckets
                if bucket.metric_name
                == MCPMetricName.TOOL_CALL_DURATION_SECONDS.value
                and bucket.result_category == MCPMetricResultCategory.UNKNOWN.value
            ]
            self.assertEqual([bucket.value for bucket in totals], [1])
            self.assertEqual([bucket.value for bucket in durations], [1])
            await runtime.shutdown()

    async def test_remote_worker_persists_terminal_remote_task_metrics(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(
                os.environ,
                {
                    "MAF_STATE_STORE_BACKEND": "sqlite",
                    "MAF_USER_MCP_MAX_ACTIVE_CALLS": "2",
                    "MAF_USER_MCP_TEMPORARY_DISK_LOW_WATERMARK_BYTES": "1",
                    "MCP_USER_SCOPED_GATEWAY_ENABLED": "true",
                    "MCP_ROUTING_MODE": "enforce",
                    "MCP_LEGACY_GLOBAL_RUNTIME_ENABLED": "true",
                    "MCP_ENFORCE_PERCENT": "100",
                    "MCP_ENFORCE_HASH_SALT": "test-only-rollout-salt",
                    "MCP_ROLLOUT_ENVIRONMENT_ID": "test-environment",
                    "MCP_ROLLOUT_DEPLOYMENT_ID": "test-deployment",
                    "MCP_ROLLOUT_STAGE": "full_enforce",
                    "MCP_ROLLOUT_ACTIVATION_ID": "test-activation",
                    "MCP_ROLLOUT_INSTANCE_ID": "test-instance",
                },
                clear=False,
            ),
        ):
            async def planner_text_generator(_prompt: str) -> str:
                return "{}"

            runtime = self._build_runtime(
                Path(directory),
                planner_text_generator=planner_text_generator,
            )
            now = datetime(2026, 8, 13, 12, 2, 30, tzinfo=timezone.utc)
            await runtime.storage.save_conversation(Conversation("conv-a", "alice"))
            await runtime.storage.save_task(
                Task(
                    "task-a",
                    "conv-a",
                    "message-a",
                    mcp_execution_mode="user_scoped",
                    mcp_shadow_enabled=False,
                    mcp_rollout_config_version=runtime.mcp_rollout_config.config_version,
                    mcp_route_reason_code="enforce_selected",
                    mcp_rollout_mode="enforce",
                )
            )
            await runtime.storage.create_user_mcp_server(
                UserMCPServer(
                    server_id="server-a",
                    owner_user_id="alice",
                    display_name="Private server",
                    routing_description="private",
                    endpoint_url="https://example.test/mcp",
                    transport=UserMCPTransport.STREAMABLE_HTTP,
                    created_at=now,
                    updated_at=now,
                ),
                None,
            )
            await runtime.storage.save_mcp_branch_record(
                MCPBranchRecord(
                    branch_id="branch-a",
                    owner_user_id="alice",
                    task_id="task-a",
                    node_id="node-a",
                    status="running",
                    created_at=now - timedelta(minutes=2),
                    updated_at=now - timedelta(minutes=2),
                )
            )
            call = MCPCallRecord(
                call_ref="safe-call-a",
                branch_id="branch-a",
                owner_user_id="alice",
                task_id="task-a",
                node_id="node-a",
                server_id="server-a",
                tool_name="private-tool",
                status="active",
                call_sequence=1,
                arguments_sha256="arguments-hash",
                server_security_version=1,
                input_schema_sha256="schema-hash",
                protocol_version="2026-07-28",
                may_have_dispatched=True,
                created_at=now - timedelta(minutes=2),
                updated_at=now - timedelta(minutes=2),
            )
            self.assertTrue(await runtime.storage.reserve_mcp_call(call))
            binding = MCPRemoteTaskBinding(
                safe_remote_task_ref="safe-remote-a",
                owner_user_id="alice",
                task_id="task-a",
                node_id="node-a",
                call_ref=call.call_ref,
                server_id="server-a",
                protocol_version="2026-07-28",
                remote_task_ciphertext=b"private",
                remote_task_nonce=b"private",
                encryption_version=1,
                last_status="completed",
                created_at=now - timedelta(minutes=2),
                updated_at=now,
                terminal_at=now,
            )
            sink = runtime.mcp_remote_task_recovery_worker._terminal_metric_sink
            self.assertIsNotNone(sink)
            await sink(
                MCPRemoteTaskTerminalMetricSample(
                    binding=binding,
                    result_category=MCPMetricResultCategory.SUCCEEDED,
                    error_category=MCPMetricErrorCategory.NONE,
                    duration_seconds=120.0,
                    terminal_at=now,
                )
            )
            await sink(
                MCPRemoteTaskTerminalMetricSample(
                    binding=replace(binding, last_status="unknown"),
                    result_category=MCPMetricResultCategory.UNKNOWN,
                    error_category=MCPMetricErrorCategory.UNKNOWN,
                    duration_seconds=120.0,
                    terminal_at=now,
                )
            )

            buckets = await runtime.storage.list_mcp_rollout_metric_buckets(
                "test-environment",
                "test-deployment",
                "full_enforce",
                window_started_at=now.replace(second=0, microsecond=0),
                window_ended_at=now.replace(second=0, microsecond=0)
                + timedelta(minutes=1),
            )
            self.assertEqual(
                {bucket.metric_name for bucket in buckets},
                {
                    MCPMetricName.TOOL_CALLS_TOTAL.value,
                    MCPMetricName.TOOL_CALL_DURATION_SECONDS.value,
                    MCPMetricName.TOOL_CALL_UNKNOWN_TOTAL.value,
                },
            )
            self.assertEqual({bucket.call_kind for bucket in buckets}, {"remote_task"})
            self.assertEqual({bucket.protocol_version for bucket in buckets}, {"2026-07-28"})
            self.assertEqual({bucket.adapter for bucket in buckets}, {"python_2026"})
            self.assertEqual(
                {bucket.result_category for bucket in buckets},
                {"succeeded", "unknown"},
            )
            unknown = [
                bucket
                for bucket in buckets
                if bucket.metric_name == MCPMetricName.TOOL_CALL_UNKNOWN_TOTAL.value
            ]
            self.assertEqual(len(unknown), 1)
            self.assertEqual(unknown[0].value, 1)

            active_binding = replace(
                binding,
                last_status="working",
                next_poll_at=now.replace(tzinfo=None),
                terminal_at=None,
            )
            await runtime.storage.save_mcp_remote_task_binding(active_binding)
            active_sink = runtime.mcp_remote_task_recovery_worker._active_metric_sink
            self.assertIsNotNone(active_sink)
            gauge_minute = datetime.now(timezone.utc).replace(second=0, microsecond=0)
            await active_sink()
            active_buckets = await runtime.storage.list_mcp_rollout_metric_buckets(
                "test-environment",
                "test-deployment",
                "full_enforce",
                window_started_at=gauge_minute,
                window_ended_at=gauge_minute + timedelta(minutes=1),
            )
            active = [
                bucket
                for bucket in active_buckets
                if bucket.metric_name == MCPMetricName.REMOTE_TASKS_ACTIVE.value
            ]
            self.assertEqual(len(active), 2)
            self.assertEqual(
                {bucket.protocol_version: bucket.value for bucket in active},
                {"2025-11-25": 0, "2026-07-28": 1},
            )

            claimed = await runtime.storage.claim_due_mcp_remote_task_bindings(
                claim_owner="test-worker",
                claim_token="test-claim",
                now=now.replace(tzinfo=None),
                lease_expires_at=now.replace(tzinfo=None) + timedelta(seconds=30),
            )
            self.assertEqual(len(claimed), 1)
            finished = await runtime.storage.finish_mcp_remote_task_binding(
                "alice",
                "task-a",
                active_binding.safe_remote_task_ref,
                claim_owner="test-worker",
                claim_token="test-claim",
                expected_revision=claimed[0].revision,
                remote_status="completed",
                call_status="completed",
                terminal_at=now.replace(tzinfo=None),
                result_ref=active_binding.safe_remote_task_ref,
            )
            self.assertIsNotNone(finished)
            await active_sink()
            active_buckets = await runtime.storage.list_mcp_rollout_metric_buckets(
                "test-environment",
                "test-deployment",
                "full_enforce",
                window_started_at=gauge_minute,
                window_ended_at=gauge_minute + timedelta(minutes=1),
            )
            active = [
                bucket
                for bucket in active_buckets
                if bucket.metric_name == MCPMetricName.REMOTE_TASKS_ACTIVE.value
            ]
            self.assertEqual({bucket.value for bucket in active}, {0})

            await runtime.storage.save_task(
                Task(
                    "task-security-mismatch",
                    "conv-a",
                    "message-security-mismatch",
                    mcp_execution_mode="user_scoped",
                    mcp_shadow_enabled=False,
                    mcp_rollout_config_version=runtime.mcp_rollout_config.config_version,
                    mcp_route_reason_code="enforce_selected",
                    mcp_rollout_mode="enforce",
                )
            )
            await runtime.storage.save_mcp_branch_record(
                MCPBranchRecord(
                    branch_id="branch-security-mismatch",
                    owner_user_id="alice",
                    task_id="task-security-mismatch",
                    node_id="node-security-mismatch",
                    status="running",
                    created_at=now,
                    updated_at=now,
                )
            )
            self.assertTrue(
                await runtime.storage.reserve_mcp_call(
                    replace(
                        call,
                        call_ref="safe-call-security-mismatch",
                        branch_id="branch-security-mismatch",
                        task_id="task-security-mismatch",
                        node_id="node-security-mismatch",
                        call_sequence=1,
                        server_security_version=999,
                    )
                )
            )
            await runtime.storage.save_mcp_remote_task_binding(
                replace(
                    active_binding,
                    safe_remote_task_ref="safe-remote-security-mismatch",
                    task_id="task-security-mismatch",
                    call_ref="safe-call-security-mismatch",
                    node_id="node-security-mismatch",
                )
            )
            await active_sink()
            active_buckets = await runtime.storage.list_mcp_rollout_metric_buckets(
                "test-environment",
                "test-deployment",
                "full_enforce",
                window_started_at=gauge_minute,
                window_ended_at=gauge_minute + timedelta(minutes=1),
            )
            active = [
                bucket
                for bucket in active_buckets
                if bucket.metric_name == MCPMetricName.REMOTE_TASKS_ACTIVE.value
            ]
            self.assertEqual({bucket.value for bucket in active}, {0})
            await runtime.shutdown()
