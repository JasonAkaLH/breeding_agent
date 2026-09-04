from __future__ import annotations

import asyncio
import json
import os
import tempfile
from functools import partial
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from src.api.dto import SubmitMessageRequest
from src.api.runtime import ApiRuntime, build_api_runtime as _build_api_runtime
from src.capabilities.mcp_dispatch.models import (
    MCPSelectorAction,
    MCPSelectorActionType,
)
from src.capabilities.mcp_dispatch.selector import MCPToolSelector
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
    InterruptAnswer,
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
from src.integrations.mcp.adapter_2026 import MCPTaskState as MCP2026TaskState
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
from src.integrations.mcp.pending_action_payloads import (
    pending_action_payload_identity,
)
from src.integrations.mcp.result_parsing.service import MCPIsolatedResultService
from src.integrations.mcp.temporary_results import MCPTemporaryResultRef
from src.integrations.token_counter import TokenizationError
from src.orchestration.agent_loop.models import (
    AgentModelBinding,
    AgentRun,
    AgentRunStatus,
)
from tests.api.support import InMemoryTaskRuntimeSidecar
from tests.master_key_support import recovery_cipher

build_api_runtime = partial(
    _build_api_runtime,
    skill_roots=(),
    public_skill_roots=(),
)


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

    def _build_runtime(self, root: Path, *, main_agent_stream_generator=None):
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
            main_agent_stream_generator=main_agent_stream_generator,
            enable_conversation_title_llm=False,
            enable_conversation_memory=False,
            runtime_sidecar_client=InMemoryTaskRuntimeSidecar(),
        )

    async def _dispatch_mcp_approval_candidate(
        self,
        runtime,
        *,
        suffix: str,
        selector=None,
        input_schema=None,
    ):
        class OneCallSelector:
            async def select(self, _context):
                return MCPSelectorAction(
                    MCPSelectorActionType.CALL_TOOL,
                    tool_name="lookup",
                    arguments={"query": f"approved-{suffix}"},
                )

        class ApprovalGateway:
            def __init__(self) -> None:
                self.call_count = 0
                self.catalog = ToolCatalogSnapshot(
                    server_id=f"server-{suffix}",
                    effective_protocol_version="2026-07-28",
                    tools=(
                        MCPToolDescriptor(
                            name="lookup",
                            description="lookup",
                            input_schema=input_schema
                            or {
                                "type": "object",
                                "properties": {"query": {"type": "string"}},
                            },
                            input_schema_sha256=f"schema-{suffix}",
                        ),
                    ),
                )

            async def open_scope(
                self, principal, platform_task_id, server_id, **_kwargs
            ):
                return MCPTaskServerScope(
                    f"scope-{suffix}",
                    principal.username,
                    platform_task_id,
                    server_id,
                    1,
                    1,
                )

            async def list_tools(self, _scope):
                return self.catalog

            async def call_tool(self, *_args, **_kwargs):
                self.call_count += 1
                raise AssertionError("approval setup must not call the tool")

            async def verify_durable_result(self, *_args, **_kwargs):
                return None

            async def close_scope(self, _scope, _reason):
                return None

        now = runtime._utcnow_naive()
        task = Task(
            task_id=f"task-{suffix}",
            conversation_id=f"conversation-{suffix}",
            root_message_id=f"message-{suffix}",
            status=TaskStatus.RUNNING,
            mcp_execution_mode="user_scoped",
            mcp_shadow_enabled=False,
            mcp_rollout_config_version="cp7",
            mcp_route_reason_code="enforce_selected",
            mcp_rollout_mode="enforce",
        )
        node = TaskNode(
            node_id=f"node-{suffix}",
            task_id=task.task_id,
            capability_id="mcp.dispatch",
            status=NodeStatus.RUNNING,
        )
        server = UserMCPServer(
            server_id=f"server-{suffix}",
            owner_user_id="alice",
            display_name=f"Approval {suffix}",
            routing_description=f"approval {suffix}",
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
                "request approval",
                task_id=task.task_id,
            )
        )
        await runtime.storage.save_task(task)
        skill_revision = runtime._skill_runtime_state.active_revision
        runtime._skill_runtime_state.retain_revision(skill_revision)
        runtime._task_skill_bundle_revisions[task.task_id] = skill_revision
        await runtime.storage.save_task_node(node)
        await runtime.storage.create_user_mcp_server(server)
        gateway = ApprovalGateway()
        coordinator = UserMCPDispatchCoordinator(
            storage=runtime.storage,
            gateway=gateway,
            selector=selector or OneCallSelector(),
            audit_reference_signer=runtime._mcp_audit_reference_signer,
            pending_action_payload_store=runtime._mcp_pending_action_payload_store,
            terminal_candidate_snapshot_authority=(
                runtime._mcp_terminal_candidate_snapshot_authority
            ),
            durable_result_snapshot_authority=(
                runtime._mcp_durable_result_snapshot_authority
            ),
            terminal_result_root=runtime._mcp_terminal_result_root,
            now_fn=lambda: now,
            terminal_now_fn=lambda: now.replace(tzinfo=timezone.utc),
        )
        outcome = await coordinator.dispatch(
            CapabilityExecutionRequest(
                capability_id="mcp.dispatch",
                conversation_id=task.conversation_id,
                task_id=task.task_id,
                node_id=node.node_id,
                input_payload={"server_id": server.server_id},
                metadata={"user_message": "request approval"},
            ),
            server_id=server.server_id,
        )
        return task, server, outcome, gateway

    async def _prepare_mcp_approval(self, runtime, *, suffix: str):
        task, server, outcome, gateway = await self._dispatch_mcp_approval_candidate(
            runtime,
            suffix=suffix,
        )
        self.assertEqual(outcome.output_payload["mcp_status"], "approval_required")
        self.assertEqual(gateway.call_count, 0)
        return task, server, outcome.interrupt

    async def test_selector_repairs_arguments_before_aggregate_approval_persistence(self) -> None:
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
            outputs = iter(
                (
                    '{"action":"call_tool","tool_name":"lookup","arguments":{}}',
                    '{"action":"call_tool","tool_name":"lookup","arguments":{"query":"repaired"}}',
                )
            )
            prompts: list[str] = []

            def generate(prompt: str) -> str:
                prompts.append(prompt)
                return next(outputs)

            task, server, outcome, gateway = (
                await self._dispatch_mcp_approval_candidate(
                    runtime,
                    suffix="selector-schema-repair",
                    selector=MCPToolSelector(text_generator=generate),
                    input_schema={
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                    },
                )
            )

            self.assertEqual(outcome.output_payload["mcp_status"], "approval_required")
            self.assertEqual(len(prompts), 2)
            self.assertEqual(gateway.call_count, 0)
            self.assertEqual(
                len(await runtime.storage.list_interrupts_for_task(task.task_id)),
                1,
            )
            self.assertEqual(
                await runtime.storage.list_user_mcp_tool_grants(
                    "alice", server.server_id
                ),
                [],
            )
            self.assertEqual(
                await runtime.storage.list_mcp_call_records("alice", task.task_id),
                [],
            )
            pending = await runtime.storage.get_mcp_pending_tool_action_for_interrupt(
                outcome.interrupt.interrupt_id
            )
            self.assertIsNotNone(pending)
            async with runtime._mcp_pending_action_payload_store.open_validated(
                pending_action_payload_identity(pending),
                pending.arguments_payload_ref,
            ) as payload:
                self.assertEqual(dict(payload.arguments), {"query": "repaired"})
            await runtime.shutdown()

    async def test_selector_double_schema_failure_has_zero_aggregate_side_effects(self) -> None:
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
            outputs = iter(
                (
                    '{"action":"call_tool","tool_name":"lookup","arguments":{}}',
                    '{"action":"call_tool","tool_name":"lookup","arguments":{}}',
                )
            )
            task, server, outcome, gateway = (
                await self._dispatch_mcp_approval_candidate(
                    runtime,
                    suffix="selector-schema-double-failure",
                    selector=MCPToolSelector(
                        text_generator=lambda _prompt: next(outputs)
                    ),
                    input_schema={
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                    },
                )
            )

            self.assertEqual(outcome.error.code, "selector_invalid_output")
            self.assertEqual(gateway.call_count, 0)
            self.assertEqual(
                await runtime.storage.list_interrupts_for_task(task.task_id),
                [],
            )
            self.assertEqual(
                await runtime.storage.list_user_mcp_tool_grants(
                    "alice", server.server_id
                ),
                [],
            )
            self.assertEqual(
                await runtime.storage.list_mcp_call_records("alice", task.task_id),
                [],
            )
            self.assertEqual(
                await runtime.storage.list_protected_mcp_pending_action_payload_refs(),
                (),
            )
            self.assertEqual(
                list(runtime._mcp_pending_action_payload_store._root.glob("*.bin")),
                [],
            )
            await runtime.shutdown()

    async def test_approval_decided_event_is_persisted_before_resume(self) -> None:
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
            task, _server, interrupt = await self._prepare_mcp_approval(
                runtime, suffix="approval-event-before-resume"
            )
            resume_entered = asyncio.Event()
            release_resume = asyncio.Event()

            async def blocked_resume(**_kwargs):
                resume_entered.set()
                await release_resume.wait()
                return True

            runtime._resume_agent_interrupt = AsyncMock(side_effect=blocked_resume)
            submission = asyncio.create_task(
                runtime.submit_chat_message(
                    task.conversation_id,
                    SubmitMessageRequest(
                        conversation_id=task.conversation_id,
                        content="仅允许本次 MCP 工具调用",
                        client_message_id=(
                            f"mcp-approval-answer-v1-{interrupt.interrupt_id}"
                        ),
                        metadata={
                            "interrupt_id": interrupt.interrupt_id,
                            "mcp_tool_approval": "allow_once",
                        },
                    ),
                    authenticated_username="alice",
                )
            )
            await asyncio.wait_for(resume_entered.wait(), timeout=2)
            events = await runtime.storage.list_events_for_task_filtered(
                task.task_id,
                event_types=("mcp.tool_approval_decided",),
            )
            self.assertEqual(len(events), 1)
            self.assertEqual(set(events[0].payload), {
                "interrupt_id",
                "safe_call_ref",
                "decision",
            })
            self.assertEqual(events[0].payload["interrupt_id"], interrupt.interrupt_id)
            self.assertEqual(events[0].payload["decision"], "allow_once")
            safe_call_ref = str(events[0].payload["safe_call_ref"])
            self.assertEqual(len(safe_call_ref), 64)
            int(safe_call_ref, 16)
            serialized = json.dumps(events[0].payload, sort_keys=True).lower()
            for forbidden in (
                "arguments",
                "endpoint",
                "credential",
                "authorization",
                "fingerprint",
                "approved-approval-event-before-resume",
            ):
                self.assertNotIn(forbidden, serialized)
            release_resume.set()
            await submission
            await runtime.shutdown()

    async def test_approval_decided_exact_replay_recovers_failed_append(self) -> None:
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
            task, server, interrupt = await self._prepare_mcp_approval(
                runtime, suffix="approval-event-exact-replay"
            )
            runtime._resume_agent_interrupt = AsyncMock(return_value=True)
            original_append_event_exact = runtime.storage.append_event_exact
            attempted_events = []
            failed_once = False

            async def fail_first_decided_append(event):
                nonlocal failed_once
                if event.event_type == "mcp.tool_approval_decided" and not failed_once:
                    failed_once = True
                    attempted_events.append(event)
                    raise OSError("simulated decided event append failure")
                return await original_append_event_exact(event)

            client_message_id = f"mcp-approval-answer-v1-{interrupt.interrupt_id}"
            request = SubmitMessageRequest(
                conversation_id=task.conversation_id,
                content="始终允许此 MCP 工具",
                client_message_id=client_message_id,
                metadata={
                    "interrupt_id": interrupt.interrupt_id,
                    "mcp_tool_approval": "always_allow",
                },
            )
            with patch.object(
                runtime.storage,
                "append_event_exact",
                side_effect=fail_first_decided_append,
            ):
                with self.assertRaisesRegex(
                    OSError, "simulated decided event append failure"
                ):
                    await runtime.submit_chat_message(
                        task.conversation_id,
                        request,
                        authenticated_username="alice",
                    )

            replay = await runtime.submit_chat_message(
                task.conversation_id,
                request,
                authenticated_username="alice",
            )
            self.assertEqual(replay["task_id"], task.task_id)
            answers = await runtime.storage.list_interrupt_answers(
                interrupt.interrupt_id
            )
            self.assertEqual(len(answers), 1)
            self.assertEqual(answers[0].source_message_id, client_message_id)
            grants = await runtime.storage.list_user_mcp_tool_grants(
                "alice", server.server_id
            )
            self.assertEqual(len(grants), 1)
            events = await runtime.storage.list_events_for_task_filtered(
                task.task_id,
                event_types=("mcp.tool_approval_decided",),
            )
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].payload["decision"], "always_allow")
            self.assertEqual(events[0].event_id, attempted_events[0].event_id)
            self.assertEqual(events[0].payload, attempted_events[0].payload)
            self.assertEqual(events[0].created_at, attempted_events[0].created_at)
            self.assertEqual(runtime._resume_agent_interrupt.await_count, 1)
            await runtime.shutdown()

    async def test_approval_deny_persists_decided_without_resume(self) -> None:
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
            task, _server, interrupt = await self._prepare_mcp_approval(
                runtime, suffix="approval-event-deny"
            )
            runtime._resume_agent_interrupt = AsyncMock(return_value=True)
            await runtime.submit_chat_message(
                task.conversation_id,
                SubmitMessageRequest(
                    conversation_id=task.conversation_id,
                    content="拒绝本次 MCP 工具调用",
                    client_message_id=(
                        f"mcp-approval-answer-v1-{interrupt.interrupt_id}"
                    ),
                    metadata={
                        "interrupt_id": interrupt.interrupt_id,
                        "mcp_tool_approval": "deny",
                    },
                ),
                authenticated_username="alice",
            )
            events = await runtime.storage.list_events_for_task_filtered(
                task.task_id,
                event_types=("mcp.tool_approval_decided",),
            )
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].payload["decision"], "deny")
            runtime._resume_agent_interrupt.assert_not_awaited()
            await runtime.shutdown()

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
                attachments=(),
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
            self.assertEqual(recovered_call.status, "unknown")
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
                pending_action_payload_store=(
                    runtime._mcp_pending_action_payload_store
                ),
                terminal_candidate_snapshot_authority=(
                    runtime._mcp_terminal_candidate_snapshot_authority
                ),
                durable_result_snapshot_authority=(
                    runtime._mcp_durable_result_snapshot_authority
                ),
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
            self.assertEqual(recovered_call.status, "unknown")
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

    async def test_approval_resume_executes_exact_persisted_action_without_reselection(
        self,
    ) -> None:
        class TwoStepSelector:
            def __init__(self) -> None:
                self.call_count = 0

            async def select(self, _context):
                self.call_count += 1
                if self.call_count == 1:
                    return MCPSelectorAction(
                        MCPSelectorActionType.CALL_TOOL,
                        tool_name="lookup",
                        arguments={"query": "original-approved-value"},
                    )
                if self.call_count == 2:
                    return MCPSelectorAction(
                        MCPSelectorActionType.CALL_TOOL,
                        tool_name="extract",
                        arguments={"document_ref": "second-approved-value"},
                    )
                return MCPSelectorAction(MCPSelectorActionType.FINISH)

        class DurableCompletedGateway:
            def __init__(self, result_store) -> None:
                self.result_store = result_store
                self.call_count = 0
                self.asserted_arguments = []
                self.catalog = ToolCatalogSnapshot(
                    server_id="server-approval-resume",
                    effective_protocol_version="2026-07-28",
                    tools=(
                        MCPToolDescriptor(
                            name="lookup",
                            description="lookup",
                            input_schema={
                                "type": "object",
                                "properties": {"query": {"type": "string"}},
                            },
                            input_schema_sha256="schema-approval-resume",
                        ),
                        MCPToolDescriptor(
                            name="extract",
                            description="extract",
                            input_schema={
                                "type": "object",
                                "properties": {
                                    "document_ref": {"type": "string"}
                                },
                            },
                            input_schema_sha256="schema-approval-extract",
                        ),
                    ),
                )

            async def open_scope(self, principal, platform_task_id, server_id, **_kwargs):
                return MCPTaskServerScope(
                    "scope-approval-resume",
                    principal.username,
                    platform_task_id,
                    server_id,
                    1,
                    1,
                )

            async def list_tools(self, _scope):
                return self.catalog

            async def call_tool(
                self, scope, tool_name, arguments, callbacks, **_kwargs
            ):
                self.call_count += 1
                self.asserted_arguments.append((tool_name, dict(arguments)))
                call_ref = f"call-approval-resume-{self.call_count}"
                await callbacks.on_created(call_ref)
                await callbacks.on_registered(call_ref)
                sink = self.result_store.create_sink(
                    scope.platform_task_id,
                    scope_id=scope.scope_id,
                    durable=True,
                    owner_user_id=scope.owner_user_id,
                    node_id="node-approval-resume",
                    call_ref=call_ref,
                )
                await sink.write(b'{"ok":true}')
                result = await sink.finalize()
                return MCPCallOutcome.completed(
                    result.ref,
                    byte_size=result.size_bytes,
                    result_content_sha256="sha256:" + result.sha256,
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
                task_id="task-approval-resume",
                conversation_id="conversation-approval-resume",
                root_message_id="message-approval-resume",
                status=TaskStatus.RUNNING,
                mcp_execution_mode="user_scoped",
                mcp_shadow_enabled=False,
                mcp_rollout_config_version="cp7",
                mcp_route_reason_code="enforce_selected",
                mcp_rollout_mode="enforce",
            )
            node = TaskNode(
                node_id="node-approval-resume",
                task_id=task.task_id,
                capability_id="mcp.dispatch",
                status=NodeStatus.RUNNING,
            )
            server = UserMCPServer(
                server_id="server-approval-resume",
                owner_user_id="alice",
                display_name="Approval resume server",
                routing_description="approval resume",
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
                    "run the approved lookup",
                    task_id=task.task_id,
                )
            )
            await runtime.storage.save_task(task)
            await runtime.storage.save_task_node(node)
            await runtime.storage.create_user_mcp_server(server)
            selector = TwoStepSelector()
            gateway = DurableCompletedGateway(runtime.user_mcp_result_store)
            projector = AsyncMock(
                side_effect=[RuntimeError("simulated projection failure"), None]
            )
            coordinator = UserMCPDispatchCoordinator(
                storage=runtime.storage,
                gateway=gateway,
                selector=selector,
                audit_reference_signer=runtime._mcp_audit_reference_signer,
                pending_action_payload_store=(
                    runtime._mcp_pending_action_payload_store
                ),
                terminal_candidate_snapshot_authority=(
                    runtime._mcp_terminal_candidate_snapshot_authority
                ),
                durable_result_snapshot_authority=(
                    runtime._mcp_durable_result_snapshot_authority
                ),
                result_artifact_projector=projector,
                terminal_result_root=runtime._mcp_terminal_result_root,
                now_fn=lambda: now,
                terminal_now_fn=lambda: now.replace(tzinfo=timezone.utc),
            )
            request = CapabilityExecutionRequest(
                capability_id="mcp.dispatch",
                conversation_id=task.conversation_id,
                task_id=task.task_id,
                node_id=node.node_id,
                input_payload={"server_id": server.server_id},
                metadata={"user_message": "run the approved lookup"},
            )

            waiting = await coordinator.dispatch(request, server_id=server.server_id)

            self.assertEqual(waiting.output_payload["mcp_status"], "approval_required")
            self.assertEqual(selector.call_count, 1)
            self.assertEqual(gateway.call_count, 0)
            accepted = await runtime.storage.accept_mcp_tool_approval(
                waiting.interrupt.interrupt_id,
                InterruptAnswer(
                    interrupt_answer_id="answer-approval-resume",
                    interrupt_id=waiting.interrupt.interrupt_id,
                    answer_payload={"mcp_tool_approval": "allow_once"},
                    source_message_id="answer-message-approval-resume",
                    accepted=True,
                    created_at=now,
                    accepted_at=now,
                ),
                "allow_once",
                now,
            )
            self.assertEqual(str(accepted), "accepted")

            waiting_second = await coordinator.dispatch(
                request, server_id=server.server_id
            )

            self.assertEqual(
                waiting_second.output_payload["mcp_status"], "approval_required"
            )
            self.assertEqual(gateway.call_count, 1)
            self.assertEqual(selector.call_count, 2)
            self.assertEqual(
                gateway.asserted_arguments,
                [("lookup", {"query": "original-approved-value"})],
            )
            accepted_second = await runtime.storage.accept_mcp_tool_approval(
                waiting_second.interrupt.interrupt_id,
                InterruptAnswer(
                    interrupt_answer_id="answer-approval-resume-2",
                    interrupt_id=waiting_second.interrupt.interrupt_id,
                    answer_payload={"mcp_tool_approval": "allow_once"},
                    source_message_id="answer-message-approval-resume-2",
                    accepted=True,
                    created_at=now,
                    accepted_at=now,
                ),
                "allow_once",
                now,
            )
            self.assertEqual(str(accepted_second), "accepted")

            completed = await coordinator.dispatch(request, server_id=server.server_id)

            self.assertIsNone(completed.error)
            self.assertEqual(gateway.call_count, 2)
            self.assertEqual(selector.call_count, 3)
            self.assertEqual(
                gateway.asserted_arguments,
                [
                    ("lookup", {"query": "original-approved-value"}),
                    ("extract", {"document_ref": "second-approved-value"}),
                ],
            )
            calls = await runtime.storage.list_mcp_call_records(
                "alice", task.task_id
            )
            self.assertEqual([call.status for call in calls], ["completed", "completed"])
            self.assertEqual(
                [call.args[0] for call in projector.await_args_list],
                [call.result_ref for call in calls],
            )
            self.assertEqual(
                list(runtime._mcp_pending_action_payload_store._root.glob("*.bin")),
                [],
            )
            self.assertEqual(
                [
                    str(
                        (
                            await runtime.storage.get_mcp_pending_tool_action(
                                call.pending_action_id
                            )
                        ).status
                    )
                    for call in calls
                ],
                ["consumed", "consumed"],
            )
            outbox = await runtime.storage.get_mcp_dispatch_resume_outbox(
                mcp_dispatch_resume_outbox_id(
                    mcp_no_server_intent_id(task.task_id, node_id=node.node_id)
                )
            )
            self.assertEqual(outbox.approval_round_total, 2)
            self.assertEqual(
                str((await runtime.storage.get_task_node(node.node_id)).status),
                "completed",
            )
            await runtime.shutdown()

    async def test_mrtr_answer_resumes_original_action_without_selector_or_reapproval(
        self,
    ) -> None:
        class Selector:
            def __init__(self) -> None:
                self.call_count = 0

            async def select(self, _context):
                self.call_count += 1
                if self.call_count == 1:
                    return MCPSelectorAction(
                        MCPSelectorActionType.CALL_TOOL,
                        tool_name="lookup",
                        arguments={"query": "durable-original"},
                    )
                return MCPSelectorAction(MCPSelectorActionType.FINISH)

        class MRTRGateway:
            def __init__(self, storage, result_store) -> None:
                self.recovery = MCPRecoveryService(
                    storage, recovery_cipher(b"a" * 32)
                )
                self.result_store = result_store
                self.call_count = 0
                self.catalog = ToolCatalogSnapshot(
                    server_id="server-mrtr-resume",
                    effective_protocol_version="2026-07-28",
                    tools=(
                        MCPToolDescriptor(
                            name="lookup",
                            description="lookup",
                            input_schema={
                                "type": "object",
                                "properties": {"query": {"type": "string"}},
                            },
                            input_schema_sha256="schema-mrtr-resume",
                        ),
                    ),
                )

            async def open_scope(self, principal, platform_task_id, server_id, **_kwargs):
                return MCPTaskServerScope(
                    "scope-mrtr-resume",
                    principal.username,
                    platform_task_id,
                    server_id,
                    1,
                    1,
                )

            async def list_tools(self, _scope):
                return self.catalog

            async def call_tool(
                self, scope, tool_name, arguments, callbacks, **kwargs
            ):
                self.call_count += 1
                call_ref = f"call-mrtr-resume-{self.call_count}"
                await callbacks.on_created(call_ref)
                await callbacks.on_registered(call_ref)
                if self.call_count == 1:
                    await self.recovery.save_request_state(
                        MCPRecoveryCallContext(
                            owner_user_id=scope.owner_user_id,
                            task_id=scope.platform_task_id,
                            node_id="node-mrtr-resume",
                            call_ref=call_ref,
                            pending_action_id=kwargs["pending_action_id"],
                            arguments_payload_ref=kwargs["arguments_payload_ref"],
                            arguments_sha256=kwargs["arguments_sha256"],
                        ),
                        server_id=scope.server_id,
                        protocol_version="2026-07-28",
                        sealed_state_ref="mcp-request-state:mrtr-resume",
                        request_state="opaque-request-state",
                        tool_name=tool_name,
                        arguments=arguments,
                        input_requests={"confirm": {"type": "boolean"}},
                    )
                    return MCPCallOutcome.input_required(
                        ({"type": "boolean"},),
                        "mcp-request-state:mrtr-resume",
                    )
                if (
                    kwargs.get("sealed_request_state_ref")
                    != "mcp-request-state:mrtr-resume"
                    or kwargs.get("input_responses") != {"confirm": True}
                    or dict(arguments) != {"query": "durable-original"}
                ):
                    raise AssertionError("MRTR continuation authority drifted")
                sink = self.result_store.create_sink(
                    scope.platform_task_id,
                    scope_id=scope.scope_id,
                    durable=True,
                    owner_user_id=scope.owner_user_id,
                    node_id="node-mrtr-resume",
                    call_ref=call_ref,
                )
                await sink.write(b'{"continued":true}')
                result = await sink.finalize()
                return MCPCallOutcome.completed(
                    result.ref,
                    byte_size=result.size_bytes,
                    result_content_sha256="sha256:" + result.sha256,
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
                task_id="task-mrtr-resume",
                conversation_id="conversation-mrtr-resume",
                root_message_id="message-mrtr-resume",
                status=TaskStatus.RUNNING,
                mcp_execution_mode="user_scoped",
                mcp_shadow_enabled=False,
                mcp_rollout_config_version="cp7",
                mcp_route_reason_code="enforce_selected",
                mcp_rollout_mode="enforce",
            )
            node = TaskNode(
                node_id="node-mrtr-resume",
                task_id=task.task_id,
                capability_id="mcp.dispatch",
                status=NodeStatus.RUNNING,
            )
            server = UserMCPServer(
                server_id="server-mrtr-resume",
                owner_user_id="alice",
                display_name="MRTR server",
                routing_description="MRTR",
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
                    "run MRTR",
                    task_id=task.task_id,
                )
            )
            await runtime.storage.save_task(task)
            await runtime.storage.save_task_node(node)
            await runtime.storage.create_user_mcp_server(server)
            selector = Selector()
            gateway = MRTRGateway(runtime.storage, runtime.user_mcp_result_store)
            coordinator = UserMCPDispatchCoordinator(
                storage=runtime.storage,
                gateway=gateway,
                selector=selector,
                audit_reference_signer=runtime._mcp_audit_reference_signer,
                pending_action_payload_store=runtime._mcp_pending_action_payload_store,
                terminal_candidate_snapshot_authority=(
                    runtime._mcp_terminal_candidate_snapshot_authority
                ),
                durable_result_snapshot_authority=(
                    runtime._mcp_durable_result_snapshot_authority
                ),
                terminal_result_root=runtime._mcp_terminal_result_root,
                now_fn=lambda: now,
                terminal_now_fn=lambda: now.replace(tzinfo=timezone.utc),
            )
            request = CapabilityExecutionRequest(
                capability_id="mcp.dispatch",
                conversation_id=task.conversation_id,
                task_id=task.task_id,
                node_id=node.node_id,
                input_payload={"server_id": server.server_id},
                metadata={"user_message": "run MRTR"},
            )
            approval = await coordinator.dispatch(request, server_id=server.server_id)
            await runtime.storage.accept_mcp_tool_approval(
                approval.interrupt.interrupt_id,
                InterruptAnswer(
                    interrupt_answer_id="answer-mrtr-approval",
                    interrupt_id=approval.interrupt.interrupt_id,
                    answer_payload={"mcp_tool_approval": "allow_once"},
                    source_message_id="answer-message-mrtr-approval",
                    accepted=True,
                    created_at=now,
                    accepted_at=now,
                ),
                "allow_once",
                now,
            )

            input_required = await coordinator.dispatch(
                request, server_id=server.server_id
            )

            self.assertEqual(input_required.output_payload["mcp_status"], "input_required")
            self.assertEqual(selector.call_count, 1)
            self.assertEqual(gateway.call_count, 1)
            accepted = await runtime.storage.accept_mcp_mrtr_answer(
                input_required.interrupt.interrupt_id,
                InterruptAnswer(
                    interrupt_answer_id="answer-mrtr-input",
                    interrupt_id=input_required.interrupt.interrupt_id,
                    answer_payload={"mcp_input_responses": {"confirm": True}},
                    source_message_id="answer-message-mrtr-input",
                    accepted=True,
                    created_at=now,
                    accepted_at=now,
                ),
                now,
            )
            self.assertEqual(str(accepted), "accepted")

            completed = await coordinator.dispatch(request, server_id=server.server_id)

            self.assertIsNone(completed.error)
            self.assertEqual(selector.call_count, 2)
            self.assertEqual(gateway.call_count, 2)
            calls = await runtime.storage.list_mcp_call_records(
                "alice", task.task_id
            )
            self.assertEqual([call.status for call in calls], ["input_required", "completed"])
            self.assertEqual(calls[1].continuation_of_call_ref, calls[0].call_ref)
            self.assertIsNone(calls[1].pending_action_id)
            self.assertEqual(
                list(runtime._mcp_pending_action_payload_store._root.glob("*.bin")),
                [],
            )
            self.assertIsNone(
                await runtime.storage.get_mcp_sealed_state(
                    "alice", task.task_id, "mcp-request-state:mrtr-resume"
                )
            )
            await runtime.shutdown()

    async def test_remote_task_tokenization_failure_is_terminal_without_replay(self) -> None:
        class Selector:
            async def select(self, _context):
                return MCPSelectorAction(
                    MCPSelectorActionType.CALL_TOOL,
                    tool_name="start_job",
                    arguments={"job": "durable"},
                )

        class RemoteGateway:
            def __init__(self, storage) -> None:
                self.call_count = 0
                self.recovery = MCPRecoveryService(
                    storage, recovery_cipher(b"a" * 32)
                )
                self.catalog = ToolCatalogSnapshot(
                    server_id="server-remote-adopt",
                    effective_protocol_version="2026-07-28",
                    tools=(
                        MCPToolDescriptor(
                            name="start_job",
                            description="start job",
                            input_schema={"type": "object"},
                            input_schema_sha256="schema-remote-adopt",
                        ),
                    ),
                )

            async def open_scope(self, principal, platform_task_id, server_id, **_kwargs):
                return MCPTaskServerScope(
                    "scope-remote-adopt",
                    principal.username,
                    platform_task_id,
                    server_id,
                    1,
                    1,
                )

            async def list_tools(self, _scope):
                return self.catalog

            async def call_tool(
                self, scope, tool_name, arguments, callbacks, **kwargs
            ):
                self.call_count += 1
                call_ref = "call-remote-adopt"
                await callbacks.on_created(call_ref)
                await callbacks.on_registered(call_ref)
                await self.recovery.save_remote_task(
                    MCPRecoveryCallContext(
                        owner_user_id=scope.owner_user_id,
                        task_id=scope.platform_task_id,
                        node_id="node-remote-adopt",
                        call_ref=call_ref,
                        continuation_plan=kwargs.get("continuation_plan"),
                    ),
                    server_id=scope.server_id,
                    protocol_version="2026-07-28",
                    safe_remote_task_ref="mcp-task:remote-adopt",
                    remote_task_id="remote-private-id",
                    status="working",
                    poll_interval_ms=0,
                )
                return MCPCallOutcome.task_created(
                    "mcp-task:remote-adopt", status="working"
                )

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
                task_id="task-remote-adopt",
                conversation_id="conversation-remote-adopt",
                root_message_id="message-remote-adopt",
                status=TaskStatus.RUNNING,
                mcp_execution_mode="user_scoped",
                mcp_shadow_enabled=False,
                mcp_rollout_config_version="cp7",
                mcp_route_reason_code="enforce_selected",
                mcp_rollout_mode="enforce",
            )
            node = TaskNode(
                node_id="node-remote-adopt",
                task_id=task.task_id,
                capability_id="mcp.dispatch",
                status=NodeStatus.RUNNING,
            )
            server = UserMCPServer(
                server_id="server-remote-adopt",
                owner_user_id="alice",
                display_name="Remote server",
                routing_description="remote task",
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
                    "start remote job",
                    task_id=task.task_id,
                )
            )
            await runtime.storage.save_task(task)
            await runtime.storage.save_task_node(node)
            await runtime.agent_run_repository.create_run(
                AgentRun(
                    run_id="run-remote-adopt",
                    task_id=task.task_id,
                    conversation_id=task.conversation_id,
                    status=AgentRunStatus.RUNNING,
                    binding=AgentModelBinding("edition-remote-adopt"),
                )
            )
            await runtime.storage.create_user_mcp_server(server)
            await runtime.storage.save_user_mcp_tool_grant(
                UserMCPToolGrant(
                    grant_id="grant-remote-adopt",
                    owner_user_id="alice",
                    server_id=server.server_id,
                    tool_name="start_job",
                    server_security_version=server.security_version,
                    input_schema_sha256="schema-remote-adopt",
                    granted_at=now,
                )
            )
            gateway = RemoteGateway(runtime.storage)
            coordinator = UserMCPDispatchCoordinator(
                storage=runtime.storage,
                gateway=gateway,
                selector=Selector(),
                audit_reference_signer=runtime._mcp_audit_reference_signer,
                pending_action_payload_store=runtime._mcp_pending_action_payload_store,
                terminal_candidate_snapshot_authority=(
                    runtime._mcp_terminal_candidate_snapshot_authority
                ),
                durable_result_snapshot_authority=(
                    runtime._mcp_durable_result_snapshot_authority
                ),
                terminal_result_root=runtime._mcp_terminal_result_root,
                now_fn=lambda: now,
                terminal_now_fn=lambda: now.replace(tzinfo=timezone.utc),
            )

            outcome = await coordinator.dispatch(
                CapabilityExecutionRequest(
                    capability_id="mcp.dispatch",
                    conversation_id=task.conversation_id,
                    task_id=task.task_id,
                    node_id=node.node_id,
                    input_payload={"server_id": server.server_id},
                    metadata={"user_message": "start remote job"},
                ),
                server_id=server.server_id,
            )

            self.assertEqual(outcome.output_payload["mcp_status"], "remote_task_created")
            binding = await runtime.storage.get_mcp_remote_task_binding(
                "alice", task.task_id, "mcp-task:remote-adopt"
            )
            call = await runtime.storage.get_mcp_call_record(
                "alice", task.task_id, "call-remote-adopt"
            )
            intent = await runtime.storage.get_mcp_no_server_intent(
                mcp_no_server_intent_id(task.task_id, node_id=node.node_id)
            )
            outbox = await runtime.storage.get_mcp_dispatch_resume_outbox(
                mcp_dispatch_resume_outbox_id(intent.intent_id)
            )
            self.assertIsNotNone(binding.published_at)
            self.assertEqual(call.status, "remote_pending")
            self.assertEqual(str(outbox.status), "remote_pending")
            self.assertEqual(
                str((await runtime.storage.get_task_node(node.node_id)).status),
                "waiting_for_dependency",
            )

            test_case = self

            class CompletedRemoteClient:
                async def tasks_get(self, safe_ref, *, recovery_context):
                    test_case.assertEqual(safe_ref, "mcp-task:remote-adopt")
                    test_case.assertEqual(
                        recovery_context.call_ref, "call-remote-adopt"
                    )
                    return MCP2026TaskState(
                        safe_remote_task_ref=safe_ref,
                        status="completed",
                        terminal=True,
                        result={
                            "resultType": "complete",
                            "content": [{"type": "text", "text": "done"}],
                        },
                    )

                async def aclose(self):
                    return None

            runtime.mcp_remote_task_recovery_worker._client_factory = (
                lambda _binding: CompletedRemoteClient()
            )
            runtime.mcp_remote_task_recovery_worker._continuation_sink = None

            tokenization = AsyncMock(
                side_effect=TokenizationError("provider unavailable")
            )
            with patch.object(
                MCPIsolatedResultService,
                "stage_projection",
                tokenization,
            ):
                self.assertEqual(
                    await runtime.mcp_remote_task_recovery_worker.run_once(), 1
                )
                self.assertEqual(
                    await runtime.mcp_remote_task_recovery_worker.run_once(), 0
                )
            tokenization.assert_awaited_once()
            self.assertEqual(gateway.call_count, 1)
            self.assertEqual(
                list(runtime._mcp_pending_action_payload_store._root.glob("*.bin")),
                [],
            )
            failed_task = await runtime.storage.get_task(task.task_id)
            failed_run = await runtime.agent_run_repository.get_run_for_task(
                task.task_id
            )
            completed_call = await runtime.storage.get_mcp_call_record(
                "alice", task.task_id, "call-remote-adopt"
            )
            self.assertEqual(str(failed_task.status), "failed")
            self.assertEqual(
                failed_run.terminal_reason_code, "model_unavailable"
            )
            self.assertEqual(completed_call.status, "completed")
            failure_events = await runtime.storage.list_events_for_task_filtered(
                task.task_id,
                event_types={"agent.run.failed", "task.failed"},
                limit=4,
            )
            self.assertEqual(
                [event.event_type for event in failure_events],
                ["agent.run.failed", "task.failed"],
            )
            self.assertEqual(
                {event.payload["code"] for event in failure_events},
                {"model_unavailable"},
            )
            artifacts = await runtime.storage.list_artifacts_for_task(task.task_id)
            self.assertEqual(len(artifacts), 1)
            artifact_metadata = json.loads(artifacts[0].storage_ref)
            self.assertEqual(artifact_metadata["source_kind"], "mcp_result")
            self.assertEqual(artifact_metadata["mime_type"], "application/json")
            projection_events = await runtime.storage.list_events_for_task_filtered(
                task.task_id,
                event_types={"mcp.result_artifact_projection"},
                limit=2,
            )
            self.assertEqual(len(projection_events), 1)
            self.assertEqual(projection_events[0].payload["status"], "ready")
            self.assertNotIn("remote-private-id", str(projection_events[0].payload))
            await runtime.shutdown()

    async def test_legacy_v2_open_interrupt_never_recovers_without_agent_locator(
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
                attachments=(),
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

            await runtime._reconcile_cp7_mcp_authority()
            outbox = await runtime.storage.get_mcp_dispatch_resume_outbox(
                mcp_dispatch_resume_outbox_id(intent_id)
            )
            self.assertEqual(str(outbox.status), "pending")

            await runtime.storage.save_interrupt(
                replace(interrupt, status=InterruptStatus.ANSWERED)
            )
            await runtime.storage.save_task_node(
                replace(node, status=NodeStatus.READY)
            )
            task = await runtime.storage.save_task(
                replace(task, cancel_requested_at=now)
            )
            await runtime._reconcile_cp7_mcp_authority()
            outbox = await runtime.storage.get_mcp_dispatch_resume_outbox(
                mcp_dispatch_resume_outbox_id(intent_id)
            )
            self.assertEqual(str(outbox.status), "pending")
            task = await runtime.storage.save_task(
                replace(task, cancel_requested_at=None)
            )
            with self.assertRaisesRegex(
                RuntimeError,
                "agent_continuation_locator_missing",
            ):
                await runtime._reconcile_cp7_mcp_authority()
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
                    "authority_digest": "a" * 64,
                    "call_item_id": "call-item-2025",
                    "capability_id": "mcp.dispatch",
                    "conversation_id": "conv-2025",
                    "model_binding": {
                        "model_edition": "test-edition",
                        "option_digests": {},
                        "reasoning_effort": "minimal",
                        "thinking_enabled": False,
                    },
                    "node_id": "node-2025",
                    "owner_scope": "user:alice",
                    "pinned_bundle_revision": None,
                    "provider_call_id": "call-2025",
                    "resume_kind": "mcp_remote_task",
                    "run_id": "run-2025",
                    "sample_item_id": "sample-item-2025",
                    "task_id": "task-2025",
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
            self.assertEqual(scheduled, [])
            self.assertEqual(await runtime._run_mcp_continuation_commands_once(), 0)
            self.assertEqual(len(scheduled), 0)
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
            async def main_agent_stream_generator(_prompt: str) -> str:
                return "{}"

            runtime = self._build_runtime(
                Path(directory),
                main_agent_stream_generator=main_agent_stream_generator,
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
            async def main_agent_stream_generator(_prompt: str) -> str:
                return "{}"

            runtime = self._build_runtime(
                Path(directory), main_agent_stream_generator=main_agent_stream_generator
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
            async def main_agent_stream_generator(_prompt: str) -> str:
                return "{}"

            runtime = self._build_runtime(
                Path(directory),
                main_agent_stream_generator=main_agent_stream_generator,
            )
            metric_now = datetime(2026, 8, 13, 12, 2, 30, tzinfo=timezone.utc)
            state_now = metric_now.replace(tzinfo=None)
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
                    created_at=state_now,
                    updated_at=state_now,
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
                    created_at=state_now - timedelta(minutes=2),
                    updated_at=state_now - timedelta(minutes=2),
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
                created_at=state_now - timedelta(minutes=2),
                updated_at=state_now - timedelta(minutes=2),
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
                created_at=state_now - timedelta(minutes=2),
                updated_at=state_now,
                terminal_at=state_now,
            )
            sink = runtime.mcp_remote_task_recovery_worker._terminal_metric_sink
            self.assertIsNotNone(sink)
            await sink(
                MCPRemoteTaskTerminalMetricSample(
                    binding=binding,
                    result_category=MCPMetricResultCategory.SUCCEEDED,
                    error_category=MCPMetricErrorCategory.NONE,
                    duration_seconds=120.0,
                    terminal_at=metric_now,
                )
            )
            await sink(
                MCPRemoteTaskTerminalMetricSample(
                    binding=replace(binding, last_status="unknown"),
                    result_category=MCPMetricResultCategory.UNKNOWN,
                    error_category=MCPMetricErrorCategory.UNKNOWN,
                    duration_seconds=120.0,
                    terminal_at=metric_now,
                )
            )

            buckets = await runtime.storage.list_mcp_rollout_metric_buckets(
                "test-environment",
                "test-deployment",
                "full_enforce",
                window_started_at=metric_now.replace(second=0, microsecond=0),
                window_ended_at=metric_now.replace(second=0, microsecond=0)
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
                next_poll_at=state_now,
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
                now=state_now,
                lease_expires_at=state_now + timedelta(seconds=30),
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
                terminal_at=state_now,
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
                    created_at=state_now,
                    updated_at=state_now,
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
