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
from src.capabilities.mcp_dispatch.models import (
    MCPSelectorAction,
    MCPSelectorActionType,
)
from src.core.contracts import CapabilityExecutionRequest
from src.core.enums import (
    InterruptStatus,
    MessageRole,
    NodeStatus,
    TaskStatus,
    UserMCPAuthType,
    UserMCPHealthStatus,
    UserMCPProtocolPreference,
    UserMCPTransport,
)
from src.core.models import (
    Conversation,
    EventRecord,
    Interrupt,
    Message,
    MCPBranchRecord,
    MCPCallRecord,
    MCPRemoteTaskBinding,
    Task,
    TaskNode,
    UserMCPCredentialRecord,
    UserMCPServer,
    UserMCPToolGrant,
)
from src.integrations.mcp.cp7_artifacts import (
    mcp_dispatch_resume_outbox_id,
    mcp_no_server_intent_id,
)
from src.integrations.mcp.resume_envelope import (
    build_mcp_dispatch_resume_envelope_v2,
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
from src.integrations.mcp.dispatch_coordinator import UserMCPDispatchCoordinator
from src.integrations.mcp.endpoint_policy import EndpointPolicyError
from src.integrations.mcp.rollout_evidence import (
    MCPMetricErrorCategory,
    MCPMetricExecutionPath,
    MCPMetricName,
    MCPMetricResultCategory,
    MCPMetricRoutingMode,
    MCPSafetyRedLine,
)
from src.integrations.mcp.gateway_models import (
    MCPCallOutcome,
    MCPTaskServerScope,
    MCPToolDescriptor,
    ToolCatalogSnapshot,
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

    async def test_terminal_task_with_active_dispatch_converges_unknown_without_replay(
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
            task = Task(
                task_id="task-terminal-active",
                conversation_id="conversation-terminal-active",
                root_message_id="message-terminal-active",
                status=TaskStatus.RUNNING,
                mcp_execution_mode="user_scoped",
                mcp_shadow_enabled=False,
                mcp_rollout_config_version="cp7",
                mcp_route_reason_code="enforce_selected",
                mcp_rollout_mode="enforce",
            )
            node = TaskNode(
                node_id="node-terminal-active",
                task_id=task.task_id,
                capability_id="mcp.dispatch",
                status=NodeStatus.RUNNING,
            )
            server = UserMCPServer(
                server_id="server-terminal-active",
                owner_user_id="alice",
                display_name="Terminal recovery server",
                routing_description="terminal recovery",
                endpoint_url="https://example.test/mcp",
                transport=UserMCPTransport.STREAMABLE_HTTP,
                health_status=UserMCPHealthStatus.AVAILABLE,
                created_at=now,
                updated_at=now,
            )
            await runtime.storage.save_conversation(
                Conversation(task.conversation_id, "alice")
            )
            await runtime.storage.save_message(
                Message(
                    task.root_message_id,
                    task.conversation_id,
                    MessageRole.USER,
                    "recover terminal authority",
                    task_id=task.task_id,
                )
            )
            await runtime.storage.save_task(task)
            await runtime.storage.save_task_node(node)
            await runtime.storage.create_user_mcp_server(server)
            await runtime.storage.save_mcp_branch_record(
                MCPBranchRecord(
                    branch_id="branch-terminal-active",
                    owner_user_id="alice",
                    task_id=task.task_id,
                    node_id=node.node_id,
                    status="running",
                    created_at=now,
                    updated_at=now,
                )
            )
            envelope = build_mcp_dispatch_resume_envelope_v2(
                task=task,
                node=node,
                edges=(),
                attachments=(),
                dependency_nodes=(),
                server_id=server.server_id,
            )
            await runtime.storage.arm_user_mcp_target_intent(
                task.task_id, node.node_id, server.server_id, envelope, now
            )
            intent_id = mcp_no_server_intent_id(task.task_id, node_id=node.node_id)
            await runtime.storage.resolve_user_mcp_target_intent(intent_id, now)
            outbox_id = mcp_dispatch_resume_outbox_id(intent_id)
            await runtime.storage.claim_mcp_dispatch_resume_outbox(
                outbox_id,
                "crashed-worker",
                "crashed-token",
                now,
                now + timedelta(minutes=5),
            )
            intent = await runtime.storage.get_mcp_no_server_intent(intent_id)
            outbox = await runtime.storage.get_mcp_dispatch_resume_outbox(outbox_id)
            self.assertTrue(
                await runtime.storage.admit_mcp_tool_call(
                    intent_id,
                    outbox_id,
                    intent.revision,
                    outbox.revision,
                    MCPCallRecord(
                        call_ref="call-terminal-active",
                        branch_id="branch-terminal-active",
                        owner_user_id="alice",
                        task_id=task.task_id,
                        node_id=node.node_id,
                        server_id=server.server_id,
                        tool_name="lookup",
                        status="reserved",
                        call_sequence=1,
                        arguments_sha256="arguments-sha",
                        server_security_version=server.security_version,
                        server_config_version=server.config_version,
                        input_schema_sha256="schema-sha",
                        protocol_version="2026-07-28",
                        created_at=now,
                        updated_at=now,
                    ),
                    now,
                )
            )
            await runtime.storage.append_event(
                EventRecord(
                    event_id="existing-task-failed",
                    conversation_id=task.conversation_id,
                    task_id=task.task_id,
                    node_id=node.node_id,
                    event_type="task.failed",
                    payload={"code": "existing_failure"},
                    created_at=now,
                )
            )
            self.assertIsNotNone(
                await runtime.storage.compare_and_set_task(
                    replace(task, status=TaskStatus.FAILED, updated_at=now),
                    expected_from_status=TaskStatus.RUNNING,
                )
            )
            network_spy = AsyncMock()
            runtime.user_mcp_gateway.call_tool = network_spy

            await runtime._reconcile_cp7_mcp_authority()

            network_spy.assert_not_awaited()
            recovered_intent = await runtime.storage.get_mcp_no_server_intent(
                intent_id
            )
            recovered_outbox = await runtime.storage.get_mcp_dispatch_resume_outbox(
                outbox_id
            )
            recovered_call = await runtime.storage.get_mcp_call_record(
                "alice", task.task_id, "call-terminal-active"
            )
            self.assertEqual(str(recovered_intent.status), "unknown")
            self.assertEqual(str(recovered_outbox.status), "completed")
            self.assertEqual(recovered_outbox.completion_mode, "unknown_no_replay")
            self.assertEqual(recovered_call.status, "execution_status_unknown")
            self.assertEqual(
                str((await runtime.storage.get_task_node(node.node_id)).status),
                "failed",
            )
            events = await runtime.storage.list_events_for_task(task.task_id)
            self.assertEqual(
                [event.event_id for event in events if event.event_type == "task.failed"],
                ["existing-task-failed"],
            )
            self.assertEqual(
                len(
                    [
                        event
                        for event in events
                        if event.event_type == "mcp.execution_status_unknown"
                    ]
                ),
                1,
            )
            audits = await runtime.storage.list_mcp_audit_events(
                "alice", task_id=task.task_id
            )
            self.assertEqual(
                [
                    audit.event_type
                    for audit in audits
                    if audit.event_type == "mcp.authority_terminal_reconciled"
                ],
                ["mcp.authority_terminal_reconciled"],
            )
            await runtime.shutdown()

    async def test_candidate_seal_failure_converges_admitted_call_immediately(
        self,
    ) -> None:
        class OneCallSelector:
            async def select(self, _context):
                return MCPSelectorAction(
                    MCPSelectorActionType.CALL_TOOL,
                    tool_name="lookup",
                    arguments={"query": "alice"},
                )

        class CompletedGateway:
            def __init__(self) -> None:
                self.call_count = 0
                self.catalog = ToolCatalogSnapshot(
                    server_id="server-seal-failure",
                    effective_protocol_version="2026-07-28",
                    tools=(
                        MCPToolDescriptor(
                            name="lookup",
                            description="lookup",
                            input_schema={
                                "type": "object",
                                "properties": {"query": {"type": "string"}},
                            },
                            input_schema_sha256="schema-seal-failure",
                        ),
                    ),
                )

            async def open_scope(self, principal, platform_task_id, server_id, **_kwargs):
                return MCPTaskServerScope(
                    "scope-seal-failure",
                    principal.username,
                    platform_task_id,
                    server_id,
                    1,
                    1,
                )

            async def list_tools(self, _scope):
                return self.catalog

            async def call_tool(
                self, _scope, _tool_name, _arguments, callbacks, **_kwargs
            ):
                self.call_count += 1
                await callbacks.on_created("call-seal-failure")
                await callbacks.on_registered("call-seal-failure")
                return MCPCallOutcome.completed(
                    "mcp-result-seal-failure",
                    byte_size=2,
                    result_content_sha256="sha256:" + "a" * 64,
                    result_store_kind="durable_content_addressed",
                )

            async def verify_durable_result(self, *_args, **_kwargs):
                return None

            async def close_scope(self, _scope, _reason):
                return None

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
            task = Task(
                task_id="task-seal-failure",
                conversation_id="conversation-seal-failure",
                root_message_id="message-seal-failure",
                status=TaskStatus.RUNNING,
                mcp_execution_mode="user_scoped",
                mcp_shadow_enabled=False,
                mcp_rollout_config_version="cp7",
                mcp_route_reason_code="enforce_selected",
                mcp_rollout_mode="enforce",
            )
            node = TaskNode(
                node_id="node-seal-failure",
                task_id=task.task_id,
                capability_id="mcp.dispatch",
                status=NodeStatus.RUNNING,
            )
            server = UserMCPServer(
                server_id="server-seal-failure",
                owner_user_id="alice",
                display_name="Seal failure server",
                routing_description="seal failure",
                endpoint_url="https://example.test/mcp",
                transport=UserMCPTransport.STREAMABLE_HTTP,
                health_status=UserMCPHealthStatus.AVAILABLE,
                created_at=now,
                updated_at=now,
            )
            await runtime.storage.save_conversation(
                Conversation(task.conversation_id, "alice")
            )
            await runtime.storage.save_message(
                Message(
                    task.root_message_id,
                    task.conversation_id,
                    MessageRole.USER,
                    "seal a candidate",
                    task_id=task.task_id,
                )
            )
            await runtime.storage.save_task(task)
            await runtime.storage.save_task_node(node)
            await runtime.storage.create_user_mcp_server(server)
            await runtime.storage.save_user_mcp_tool_grant(
                UserMCPToolGrant(
                    grant_id="grant-seal-failure",
                    owner_user_id="alice",
                    server_id=server.server_id,
                    tool_name="lookup",
                    server_security_version=server.security_version,
                    input_schema_sha256="schema-seal-failure",
                    granted_at=now,
                )
            )
            gateway = CompletedGateway()
            coordinator = UserMCPDispatchCoordinator(
                storage=runtime.storage,
                gateway=gateway,
                selector=OneCallSelector(),
                audit_reference_signer=runtime._mcp_audit_reference_signer,
                terminal_result_root=runtime._mcp_terminal_result_root,
                now_fn=lambda: now,
                terminal_now_fn=lambda: now.replace(tzinfo=timezone.utc),
            )

            with patch(
                "src.integrations.mcp.dispatch_coordinator.seal_terminal_result_candidate",
                side_effect=OSError("simulated fsync failure"),
            ):
                outcome = await coordinator.dispatch(
                    CapabilityExecutionRequest(
                        capability_id="mcp.dispatch",
                        conversation_id=task.conversation_id,
                        task_id=task.task_id,
                        node_id=node.node_id,
                        input_payload={"server_id": server.server_id},
                        metadata={"user_message": "seal a candidate"},
                    ),
                    server_id=server.server_id,
                )

            self.assertEqual(gateway.call_count, 1)
            self.assertEqual(outcome.error.code, "mcp_terminal_candidate_seal_failed")
            recovered_call = await runtime.storage.get_mcp_call_record(
                "alice", task.task_id, "call-seal-failure"
            )
            self.assertEqual(recovered_call.status, "execution_status_unknown")
            self.assertEqual(
                str((await runtime.storage.get_task(task.task_id)).status), "failed"
            )
            self.assertEqual(
                str((await runtime.storage.get_task_node(node.node_id)).status),
                "failed",
            )
            intent = await runtime.storage.get_mcp_no_server_intent(
                mcp_no_server_intent_id(task.task_id, node_id=node.node_id)
            )
            self.assertEqual(str(intent.status), "unknown")
            await runtime.shutdown()

    async def test_v2_open_interrupt_waits_then_recovers_automatic_metadata(
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
            task = Task(
                task_id="task-v2-open",
                conversation_id="conversation-v2-open",
                root_message_id="message-v2-open",
                status=TaskStatus.RUNNING,
                mcp_execution_mode="user_scoped",
                mcp_shadow_enabled=False,
                mcp_rollout_config_version="cp7",
                mcp_route_reason_code="enforce_selected",
                mcp_rollout_mode="enforce",
            )
            node = TaskNode(
                node_id="node-v2-open",
                task_id=task.task_id,
                capability_id="mcp.dispatch",
                status=NodeStatus.WAITING_FOR_INPUT,
            )
            server = UserMCPServer(
                server_id="server-v2-open",
                owner_user_id="alice",
                display_name="Waiting server",
                routing_description="waiting",
                endpoint_url="https://example.test/mcp",
                transport=UserMCPTransport.STREAMABLE_HTTP,
                health_status=UserMCPHealthStatus.AVAILABLE,
                created_at=now,
                updated_at=now,
            )
            await runtime.storage.save_conversation(
                Conversation(task.conversation_id, "alice")
            )
            await runtime.storage.save_message(
                Message(
                    task.root_message_id,
                    task.conversation_id,
                    MessageRole.USER,
                    "wait for my answer",
                    task_id=task.task_id,
                )
            )
            await runtime.storage.save_task(task)
            await runtime.storage.save_task_node(node)
            await runtime.storage.create_user_mcp_server(server)
            envelope = build_mcp_dispatch_resume_envelope_v2(
                task=task,
                node=node,
                edges=(),
                attachments=(),
                dependency_nodes=(),
                server_id=server.server_id,
            )
            self.assertEqual(
                str(
                    await runtime.storage.arm_user_mcp_target_intent(
                        task.task_id,
                        node.node_id,
                        server.server_id,
                        envelope,
                        now,
                    )
                ),
                "armed",
            )
            intent_id = mcp_no_server_intent_id(
                task.task_id, node_id=node.node_id
            )
            self.assertEqual(
                str(
                    await runtime.storage.resolve_user_mcp_target_intent(
                        intent_id, now
                    )
                ),
                "available",
            )
            interrupt = Interrupt(
                interrupt_id="interrupt-v2-open",
                conversation_id=task.conversation_id,
                task_id=task.task_id,
                node_id=node.node_id,
                source_agent="mcp.dispatch",
                source_message_id=task.root_message_id,
                question="continue?",
                reason_code="input_required",
                status=InterruptStatus.OPEN,
            )
            await runtime.storage.save_interrupt(interrupt)

            resume = AsyncMock()
            with patch.object(
                runtime.orchestration_service,
                "resume_persisted_mcp_dispatch_node",
                resume,
            ):
                await runtime._reconcile_cp7_mcp_authority()

            resume.assert_not_awaited()
            outbox = await runtime.storage.get_mcp_dispatch_resume_outbox(
                mcp_dispatch_resume_outbox_id(intent_id)
            )
            self.assertEqual(str(outbox.status), "pending")

            await runtime.storage.save_interrupt(
                replace(interrupt, status=InterruptStatus.ANSWERED)
            )
            ready_node = await runtime.storage.save_task_node(
                replace(node, status=NodeStatus.READY)
            )
            task = await runtime.storage.save_task(
                replace(task, cancel_requested_at=now)
            )
            cancelled_resume = AsyncMock()
            with patch.object(
                runtime.orchestration_service,
                "resume_persisted_mcp_dispatch_node",
                cancelled_resume,
            ):
                await runtime._reconcile_cp7_mcp_authority()
            cancelled_resume.assert_not_awaited()
            outbox = await runtime.storage.get_mcp_dispatch_resume_outbox(
                mcp_dispatch_resume_outbox_id(intent_id)
            )
            self.assertEqual(str(outbox.status), "pending")
            task = await runtime.storage.save_task(
                replace(task, cancel_requested_at=None)
            )
            resume = AsyncMock(
                return_value=(
                    replace(ready_node, status=NodeStatus.COMPLETED),
                    {},
                )
            )
            with patch.object(
                runtime.orchestration_service,
                "resume_persisted_mcp_dispatch_node",
                resume,
            ):
                await runtime._reconcile_cp7_mcp_authority()

            resume.assert_awaited_once()
            resume_request = resume.await_args.args[0]
            self.assertEqual(
                resume_request.metadata["mcp_binding_mode"], "automatic"
            )
            self.assertEqual(
                resume_request.metadata["user_message"], "wait for my answer"
            )
            await runtime.shutdown()

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
