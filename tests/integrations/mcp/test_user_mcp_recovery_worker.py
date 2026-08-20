from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import delete

from src.core.enums import NodeStatus
from src.core.models import (
    Conversation,
    InterruptAnswer,
    MCPBranchRecord,
    MCPCallRecord,
    MCPRemoteTaskBinding,
    Task,
    TaskNode,
)
from src.integrations.mcp.adapter_2026 import MCPTaskState
from src.integrations.mcp.adapter_2025_tasks import MCP2025TaskState
from src.integrations.mcp.credentials import MCPRecoveryCallContext
from src.integrations.mcp.protocol import MCP_PROTOCOL_VERSION_2026_07_28
from src.integrations.mcp.recovery_worker import (
    MCPContinuationAdmissionResult,
    MCPRemoteTaskProcessedResult,
    MCPRemoteTaskRecoveryError,
    MCPRemoteTaskRecoveryWorker,
    MCPRemoteTaskTerminalMetricSample,
)
from src.integrations.mcp.rollout_evidence import (
    MCPMetricErrorCategory,
    MCPMetricResultCategory,
)
from src.storage.sqlite import (
    SQLiteStorage,
    bootstrap_sqlite_database,
    create_sqlite_engine,
    create_sqlite_session_factory,
)
from src.storage.sqlite.models import MCPRemoteTaskOutboxRow


class _RecordingClient:
    def __init__(
        self,
        *,
        state: MCPTaskState | None = None,
        error: Exception | None = None,
        entered: asyncio.Event | None = None,
        release: asyncio.Event | None = None,
        control_error: Exception | None = None,
    ) -> None:
        self.state = state
        self.error = error
        self.entered = entered
        self.release = release
        self.control_error = control_error
        self.calls: list[object] = []

    async def tasks_get(
        self,
        safe_remote_task_ref: str,
        *,
        recovery_context: MCPRecoveryCallContext,
    ) -> MCPTaskState:
        self.calls.append(("tasks/get", safe_remote_task_ref, recovery_context))
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            await self.release.wait()
        if self.error is not None:
            raise self.error
        if self.state is None:
            raise AssertionError("test client has no task state")
        return self.state

    async def list_tools(self) -> None:
        raise AssertionError("recovery must not call tools/list")

    async def call_tool(self, *_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("recovery must not call tools/call")

    async def tasks_update(
        self,
        safe_remote_task_ref: str,
        input_responses: dict[str, Any],
        *,
        recovery_context: MCPRecoveryCallContext,
    ) -> None:
        self.calls.append(
            (
                "tasks/update",
                safe_remote_task_ref,
                dict(input_responses),
                recovery_context,
            )
        )
        if self.control_error is not None:
            raise self.control_error

    async def tasks_cancel(
        self,
        safe_remote_task_ref: str,
        *,
        recovery_context: MCPRecoveryCallContext,
        reason: str = "",
    ) -> None:
        self.calls.append(
            ("tasks/cancel", safe_remote_task_ref, reason, recovery_context)
        )
        if self.control_error is not None:
            raise self.control_error

    async def aclose(self) -> None:
        self.calls.append("aclose")


class UserMCPRemoteTaskRecoveryWorkerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        super().setUp()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmpdir.name) / "mcp-recovery-worker.sqlite3"
        self.engine = create_sqlite_engine(self.db_path)
        session_factory = create_sqlite_session_factory(self.engine)
        bootstrap_sqlite_database(self.engine)
        self.storage = SQLiteStorage(session_factory)
        self.now = datetime(2026, 8, 13, 12, 0, 0)

    def tearDown(self) -> None:
        self.engine.dispose()
        self._tmpdir.cleanup()
        super().tearDown()

    def _binding(
        self,
        suffix: str,
        *,
        protocol_version: str = MCP_PROTOCOL_VERSION_2026_07_28,
    ) -> MCPRemoteTaskBinding:
        return MCPRemoteTaskBinding(
            safe_remote_task_ref=f"remote-{suffix}",
            owner_user_id="alice",
            task_id=f"task-{suffix}",
            node_id=f"node-{suffix}",
            call_ref=f"call-{suffix}",
            server_id="server-a",
            protocol_version=protocol_version,
            remote_task_ciphertext=b"ciphertext",
            remote_task_nonce=b"nonce",
            encryption_version=1,
            last_status="working",
            next_poll_at=self.now,
            created_at=self.now,
            updated_at=self.now,
        )

    async def _get(self, binding: MCPRemoteTaskBinding) -> MCPRemoteTaskBinding:
        stored = await self.storage.get_mcp_remote_task_binding(
            binding.owner_user_id,
            binding.task_id,
            binding.safe_remote_task_ref,
        )
        self.assertIsNotNone(stored)
        return stored

    async def _admit_continuation(
        self,
        outbox,
        *,
        effect=None,
        fail_after_admission: bool = False,
    ) -> MCPContinuationAdmissionResult:
        admitted = outbox
        status = "already_admitted"
        if outbox.continuation_admitted_at is None:
            admitted = await self.storage.admit_mcp_remote_task_continuation(
                outbox.outbox_id,
                claim_owner=outbox.claim_owner,
                claim_token=outbox.claim_token,
                expected_revision=outbox.revision,
                admitted_at=self.now,
            )
            status = "admitted_new"
        if fail_after_admission:
            raise RuntimeError("crash after durable admission")
        if effect is not None:
            effect(admitted)
        return MCPContinuationAdmissionResult(status, admitted)

    async def _reserve_call(self, binding: MCPRemoteTaskBinding) -> None:
        await self.storage.save_conversation(
            Conversation(f"conv-{binding.task_id}", binding.owner_user_id)
        )
        await self.storage.save_task(
            Task(
                binding.task_id,
                f"conv-{binding.task_id}",
                f"message-{binding.task_id}",
            )
        )
        await self.storage.save_task_node(
            TaskNode(
                node_id=binding.node_id,
                task_id=binding.task_id,
                capability_id="mcp.dispatch",
                status=NodeStatus.WAITING_FOR_DEPENDENCY,
            )
        )
        await self.storage.save_mcp_branch_record(
            MCPBranchRecord(
                branch_id=f"branch-{binding.call_ref}",
                owner_user_id=binding.owner_user_id,
                task_id=binding.task_id,
                node_id=binding.node_id,
                status="ready",
                created_at=self.now,
                updated_at=self.now,
            )
        )
        reserved = await self.storage.reserve_mcp_call(
            MCPCallRecord(
                call_ref=binding.call_ref,
                branch_id=f"branch-{binding.call_ref}",
                owner_user_id=binding.owner_user_id,
                task_id=binding.task_id,
                node_id=binding.node_id,
                server_id=binding.server_id,
                tool_name="lookup",
                status="active",
                call_sequence=1,
                arguments_sha256="args",
                server_security_version=1,
                input_schema_sha256="schema",
                may_have_dispatched=True,
                created_at=self.now,
                updated_at=self.now,
            )
        )
        self.assertTrue(reserved)

    async def test_claim_is_exclusive_renews_and_recovery_is_query_only(self) -> None:
        binding = self._binding("exclusive")
        await self.storage.save_mcp_remote_task_binding(binding)
        entered = asyncio.Event()
        release = asyncio.Event()
        client = _RecordingClient(
            state=MCPTaskState(
                safe_remote_task_ref=binding.safe_remote_task_ref,
                status="working",
                terminal=False,
                poll_interval_ms=5_000,
            ),
            entered=entered,
            release=release,
        )
        first = MCPRemoteTaskRecoveryWorker(
            storage=self.storage,
            client_factory=lambda _binding: client,
            instance_id="worker-a",
            now_fn=lambda: self.now,
            claim_ttl_seconds=0.2,
            claim_renew_seconds=0.01,
        )
        second_factory_calls = 0

        def second_factory(_binding: MCPRemoteTaskBinding) -> _RecordingClient:
            nonlocal second_factory_calls
            second_factory_calls += 1
            raise AssertionError("a second worker must not receive the active claim")

        second = MCPRemoteTaskRecoveryWorker(
            storage=self.storage,
            client_factory=second_factory,
            instance_id="worker-b",
            now_fn=lambda: self.now,
        )

        first_run = asyncio.create_task(first.run_once())
        await asyncio.wait_for(entered.wait(), timeout=1)
        await asyncio.sleep(0.04)
        self.assertEqual(await second.run_once(), 0)
        release.set()
        self.assertEqual(await first_run, 1)

        stored = await self._get(binding)
        self.assertIsNone(stored.claim_owner)
        self.assertEqual(stored.next_poll_at, self.now + timedelta(seconds=5))
        self.assertGreater(stored.revision, 3)
        self.assertEqual(second_factory_calls, 0)
        self.assertEqual(
            client.calls[0],
            (
                "tasks/get",
                binding.safe_remote_task_ref,
                MCPRecoveryCallContext(
                    owner_user_id="alice",
                    task_id="task-exclusive",
                    node_id="node-exclusive",
                    call_ref="call-exclusive",
                ),
            ),
        )
        self.assertEqual(client.calls[-1], "aclose")

    async def test_terminal_and_input_required_states_stop_polling(self) -> None:
        terminal = self._binding("terminal")
        input_required = self._binding("input")
        await self._reserve_call(terminal)
        await self._reserve_call(input_required)
        await self.storage.save_mcp_remote_task_binding(terminal)
        await self.storage.save_mcp_remote_task_binding(input_required)
        clients = {
            terminal.safe_remote_task_ref: _RecordingClient(
                state=MCPTaskState(
                    safe_remote_task_ref=terminal.safe_remote_task_ref,
                    status="completed",
                    terminal=True,
                    result={"ok": True},
                )
            ),
            input_required.safe_remote_task_ref: _RecordingClient(
                state=MCPTaskState(
                    safe_remote_task_ref=input_required.safe_remote_task_ref,
                    status="input_required",
                    terminal=False,
                    input_requests={"approval": {"type": "boolean"}},
                )
            ),
        }
        worker = MCPRemoteTaskRecoveryWorker(
            storage=self.storage,
            client_factory=lambda binding: clients[binding.safe_remote_task_ref],
            instance_id="worker-states",
            now_fn=lambda: self.now,
            result_persister=lambda _binding, _result: "mcp-result-terminal",
        )

        self.assertEqual(await worker.run_once(), 2)

        stored_terminal = await self._get(terminal)
        self.assertEqual(stored_terminal.last_status, "completed")
        self.assertEqual(stored_terminal.terminal_at, self.now)
        self.assertIsNone(stored_terminal.next_poll_at)
        self.assertIsNone(stored_terminal.claim_owner)
        completed_call = await self.storage.get_mcp_call_record(
            terminal.owner_user_id, terminal.task_id, terminal.call_ref
        )
        self.assertEqual(completed_call.status, "completed")
        self.assertEqual(completed_call.result_ref, "mcp-result-terminal")
        self.assertNotEqual(completed_call.result_ref, terminal.safe_remote_task_ref)
        self.assertEqual(completed_call.terminal_at, self.now)
        completed_branch = await self.storage.get_mcp_branch_record(
            terminal.owner_user_id,
            terminal.task_id,
            f"branch-{terminal.call_ref}",
        )
        self.assertIsNone(completed_branch.active_call_ref)
        stored_input = await self._get(input_required)
        self.assertEqual(stored_input.last_status, "input_required")
        self.assertIsNone(stored_input.terminal_at)
        self.assertIsNone(stored_input.next_poll_at)
        self.assertIsNone(stored_input.claim_owner)
        recoverable_call = await self.storage.get_mcp_call_record(
            input_required.owner_user_id,
            input_required.task_id,
            input_required.call_ref,
        )
        self.assertEqual(recoverable_call.status, "active")
        self.assertIsNone(recoverable_call.terminal_at)
        recoverable_branch = await self.storage.get_mcp_branch_record(
            input_required.owner_user_id,
            input_required.task_id,
            f"branch-{input_required.call_ref}",
        )
        self.assertEqual(recoverable_branch.active_call_ref, input_required.call_ref)

    async def test_remote_input_required_uses_durable_tasks_update_command(self) -> None:
        binding = self._binding("input-update")
        await self._reserve_call(binding)
        await self.storage.save_mcp_remote_task_binding(binding)
        poll_client = _RecordingClient(
            state=MCPTaskState(
                safe_remote_task_ref=binding.safe_remote_task_ref,
                status="input_required",
                terminal=False,
                input_requests={"approval": {"type": "boolean"}},
            )
        )
        poller = MCPRemoteTaskRecoveryWorker(
            storage=self.storage,
            client_factory=lambda _binding: poll_client,
            instance_id="worker-input-poll",
            now_fn=lambda: self.now,
        )
        self.assertEqual(await poller.run_once(), 1)
        interrupts = await self.storage.list_interrupts_for_task(binding.task_id)
        self.assertEqual(len(interrupts), 1)
        self.assertEqual(
            interrupts[0].reason_code, "mcp_remote_task_input_required"
        )
        node = await self.storage.get_task_node(binding.node_id)
        self.assertEqual(node.status, NodeStatus.WAITING_FOR_INPUT)

        command = await self.storage.enqueue_mcp_remote_task_control(
            InterruptAnswer(
                interrupt_answer_id="answer-input-update",
                interrupt_id=interrupts[0].interrupt_id,
                answer_payload={"mcp_input_responses": {"approval": True}},
                accepted=True,
                created_at=self.now,
                accepted_at=self.now,
            ),
            action="update",
            input_responses={"approval": True},
            updated_at=self.now,
        )
        self.assertIsNotNone(command)
        control_client = _RecordingClient()
        consumer = MCPRemoteTaskRecoveryWorker(
            storage=self.storage,
            client_factory=lambda _binding: control_client,
            instance_id="worker-input-update",
            now_fn=lambda: self.now,
            continuation_sink=self._admit_continuation,
        )
        self.assertEqual(await consumer.run_once(), 0)
        stored = await self._get(binding)
        self.assertEqual(stored.last_status, "working")
        self.assertEqual(stored.next_poll_at, self.now)
        self.assertEqual(control_client.calls[0][0], "tasks/update")
        self.assertNotIn("tools/call", repr(control_client.calls))

    async def test_terminal_continuation_recovers_after_crash_without_repoll(self) -> None:
        binding = self._binding("continuation-restart")
        await self._reserve_call(binding)
        await self.storage.save_mcp_remote_task_binding(binding)
        first = MCPRemoteTaskRecoveryWorker(
            storage=self.storage,
            client_factory=lambda _binding: _RecordingClient(
                state=MCPTaskState(
                    safe_remote_task_ref=binding.safe_remote_task_ref,
                    status="completed",
                    terminal=True,
                    result={"ok": True},
                )
            ),
            instance_id="worker-continuation-first",
            now_fn=lambda: self.now,
            result_persister=lambda _binding, _result: "mcp-result-restart",
            continuation_sink=lambda outbox: self._admit_continuation(
                outbox, fail_after_admission=True
            ),
        )
        with self.assertRaisesRegex(RuntimeError, "crash after durable admission"):
            await first.run_once()
        node = await self.storage.get_task_node(binding.node_id)
        self.assertEqual(node.status, NodeStatus.COMPLETED)
        self.assertEqual(node.output_refs, ("mcp-result-restart",))

        continued: list[str] = []
        restarted_at = self.now + timedelta(seconds=31)
        second = MCPRemoteTaskRecoveryWorker(
            storage=self.storage,
            client_factory=lambda _binding: (_ for _ in ()).throw(
                AssertionError("terminal binding must not be polled after restart")
            ),
            instance_id="worker-continuation-second",
            now_fn=lambda: restarted_at,
            continuation_sink=lambda outbox: self._admit_continuation(
                outbox, effect=lambda item: continued.append(item.outbox_id)
            ),
        )
        self.assertEqual(await second.run_once(), 0)
        self.assertEqual(
            continued, [f"mcp-remote-terminal:{binding.call_ref}"]
        )
        self.assertEqual(await second.run_once(), 0)
        self.assertEqual(len(continued), 1)

    async def test_ambiguous_tasks_update_is_terminal_unknown_and_not_replayed(self) -> None:
        binding = self._binding("input-ambiguous")
        await self._reserve_call(binding)
        await self.storage.save_mcp_remote_task_binding(binding)
        poller = MCPRemoteTaskRecoveryWorker(
            storage=self.storage,
            client_factory=lambda _binding: _RecordingClient(
                state=MCPTaskState(
                    safe_remote_task_ref=binding.safe_remote_task_ref,
                    status="input_required",
                    terminal=False,
                    input_requests={"value": {"type": "string"}},
                )
            ),
            instance_id="worker-input-ambiguous-poll",
            now_fn=lambda: self.now,
        )
        await poller.run_once()
        interrupt = (await self.storage.list_interrupts_for_task(binding.task_id))[0]
        await self.storage.enqueue_mcp_remote_task_control(
            InterruptAnswer(
                interrupt_answer_id="answer-input-ambiguous",
                interrupt_id=interrupt.interrupt_id,
                answer_payload={"mcp_input_responses": {"value": "x"}},
                accepted=True,
                created_at=self.now,
                accepted_at=self.now,
            ),
            action="update",
            input_responses={"value": "x"},
            updated_at=self.now,
        )
        calls: list[_RecordingClient] = []

        def factory(_binding):
            client = _RecordingClient(control_error=TimeoutError("timeout"))
            calls.append(client)
            return client

        consumer = MCPRemoteTaskRecoveryWorker(
            storage=self.storage,
            client_factory=factory,
            instance_id="worker-input-ambiguous",
            now_fn=lambda: self.now,
            continuation_sink=self._admit_continuation,
        )
        await consumer.run_once()
        stored = await self._get(binding)
        self.assertEqual(stored.last_status, "unknown")
        self.assertIsNotNone(stored.terminal_at)
        self.assertEqual(len(calls), 1)
        self.assertEqual(await consumer.run_once(), 0)
        self.assertEqual(len(calls), 1)

    async def test_abandoned_sending_control_converges_unknown_without_retransmit(self) -> None:
        binding = self._binding("input-sending-crash")
        await self._reserve_call(binding)
        await self.storage.save_mcp_remote_task_binding(binding)
        poller = MCPRemoteTaskRecoveryWorker(
            storage=self.storage,
            client_factory=lambda _binding: _RecordingClient(
                state=MCPTaskState(
                    safe_remote_task_ref=binding.safe_remote_task_ref,
                    status="input_required",
                    terminal=False,
                    input_requests={"value": {"type": "string"}},
                )
            ),
            instance_id="worker-input-sending-poll",
            now_fn=lambda: self.now,
        )
        await poller.run_once()
        interrupt = (await self.storage.list_interrupts_for_task(binding.task_id))[0]
        await self.storage.enqueue_mcp_remote_task_control(
            InterruptAnswer(
                interrupt_answer_id="answer-input-sending-crash",
                interrupt_id=interrupt.interrupt_id,
                answer_payload={"mcp_input_responses": {"value": "x"}},
                accepted=True,
                created_at=self.now,
                accepted_at=self.now,
            ),
            action="update",
            input_responses={"value": "x"},
            updated_at=self.now,
        )
        claimed = await self.storage.claim_mcp_remote_task_outbox(
            claim_owner="crashed-worker",
            claim_token="crashed-token",
            now=self.now,
            lease_expires_at=self.now + timedelta(seconds=30),
        )
        sending = await self.storage.begin_mcp_remote_task_control_delivery(
            claimed[0].outbox_id,
            claim_owner="crashed-worker",
            claim_token="crashed-token",
            expected_revision=claimed[0].revision,
            lease_expires_at=self.now + timedelta(seconds=30),
            updated_at=self.now,
        )
        self.assertEqual(sending.status, "sending")
        abandoned = await self.storage.claim_abandoned_mcp_remote_task_controls(
            claim_owner="crashed-abandoner",
            claim_token="crashed-abandon-token",
            now=self.now + timedelta(seconds=31),
        )
        self.assertEqual(len(abandoned), 1)
        self.assertEqual(abandoned[0].status, "abandoning")

        restarted = MCPRemoteTaskRecoveryWorker(
            storage=self.storage,
            client_factory=lambda _binding: (_ for _ in ()).throw(
                AssertionError("abandoned sending command must not be retransmitted")
            ),
            instance_id="worker-input-sending-restart",
            now_fn=lambda: self.now + timedelta(seconds=31),
            continuation_sink=self._admit_continuation,
        )
        await restarted.run_once()
        stored = await self._get(binding)
        self.assertEqual(stored.last_status, "unknown")
        self.assertIsNotNone(stored.terminal_at)

    async def test_remote_input_cancel_uses_tasks_cancel_and_converges(self) -> None:
        binding = self._binding("input-cancel")
        await self._reserve_call(binding)
        await self.storage.save_mcp_remote_task_binding(binding)
        poller = MCPRemoteTaskRecoveryWorker(
            storage=self.storage,
            client_factory=lambda _binding: _RecordingClient(
                state=MCPTaskState(
                    safe_remote_task_ref=binding.safe_remote_task_ref,
                    status="input_required",
                    terminal=False,
                    input_requests={"approval": {"type": "boolean"}},
                )
            ),
            instance_id="worker-input-cancel-poll",
            now_fn=lambda: self.now,
        )
        await poller.run_once()
        interrupt = (await self.storage.list_interrupts_for_task(binding.task_id))[0]
        await self.storage.enqueue_mcp_remote_task_control(
            InterruptAnswer(
                interrupt_answer_id="answer-input-cancel",
                interrupt_id=interrupt.interrupt_id,
                answer_payload={"mcp_remote_task_cancel": True},
                accepted=True,
                created_at=self.now,
                accepted_at=self.now,
            ),
            action="cancel",
            input_responses={},
            updated_at=self.now,
        )
        client = _RecordingClient()
        consumer = MCPRemoteTaskRecoveryWorker(
            storage=self.storage,
            client_factory=lambda _binding: client,
            instance_id="worker-input-cancel",
            now_fn=lambda: self.now,
            continuation_sink=self._admit_continuation,
        )
        await consumer.run_once()
        stored = await self._get(binding)
        self.assertEqual(stored.last_status, "cancelled")
        self.assertIsNotNone(stored.terminal_at)
        self.assertEqual(client.calls[0][0], "tasks/cancel")

    async def test_missing_input_outbox_does_not_mutate_interrupt_or_node(self) -> None:
        binding = self._binding("input-missing-outbox")
        await self._reserve_call(binding)
        await self.storage.save_mcp_remote_task_binding(binding)
        poller = MCPRemoteTaskRecoveryWorker(
            storage=self.storage,
            client_factory=lambda _binding: _RecordingClient(
                state=MCPTaskState(
                    safe_remote_task_ref=binding.safe_remote_task_ref,
                    status="input_required",
                    terminal=False,
                    input_requests={"value": {"type": "string"}},
                )
            ),
            instance_id="worker-input-missing-outbox",
            now_fn=lambda: self.now,
        )
        await poller.run_once()
        interrupt = (await self.storage.list_interrupts_for_task(binding.task_id))[0]
        with self.storage._session_factory() as session:
            session.execute(
                delete(MCPRemoteTaskOutboxRow).where(
                    MCPRemoteTaskOutboxRow.outbox_id
                    == f"mcp-remote-input:{binding.call_ref}"
                )
            )
            session.commit()

        command = await self.storage.enqueue_mcp_remote_task_control(
            InterruptAnswer(
                interrupt_answer_id="answer-missing-outbox",
                interrupt_id=interrupt.interrupt_id,
                answer_payload={"mcp_input_responses": {"value": "x"}},
                accepted=True,
                created_at=self.now,
                accepted_at=self.now,
            ),
            action="update",
            input_responses={"value": "x"},
            updated_at=self.now,
        )

        self.assertIsNone(command)
        node = await self.storage.get_task_node(binding.node_id)
        self.assertEqual(node.status, NodeStatus.WAITING_FOR_INPUT)
        stored_interrupt = await self.storage.get_interrupt(interrupt.interrupt_id)
        self.assertEqual(str(stored_interrupt.status), "open")

    async def test_terminal_statuses_are_normalized_without_remote_payloads(self) -> None:
        expected = {
            "failed": ("failed", "mcp_remote_task_failed"),
            "cancelled": ("cancelled", "mcp_remote_task_cancelled"),
            "vendor_secret_status": ("unknown", "execution_status_unknown"),
        }
        bindings = [self._binding(f"normalized-{index}") for index, _ in enumerate(expected)]
        for binding in bindings:
            await self._reserve_call(binding)
            await self.storage.save_mcp_remote_task_binding(binding)
        clients = {
            binding.safe_remote_task_ref: _RecordingClient(
                state=MCPTaskState(
                    safe_remote_task_ref=binding.safe_remote_task_ref,
                    status=remote_status,
                    terminal=True,
                    result={"secret": "raw-result"},
                    error={"secret": "raw-error"},
                )
            )
            for binding, remote_status in zip(bindings, expected, strict=True)
        }
        sealed: list[tuple[str, str, str | None, str | None]] = []

        async def seal(binding, call_status, result_ref, safe_error_code):
            before_finish = await self._get(binding)
            self.assertIsNone(before_finish.terminal_at)
            sealed.append(
                (binding.call_ref, call_status, result_ref, safe_error_code)
            )

        worker = MCPRemoteTaskRecoveryWorker(
            storage=self.storage,
            client_factory=lambda binding: clients[binding.safe_remote_task_ref],
            instance_id="worker-terminal-normalization",
            now_fn=lambda: self.now,
            terminal_sealer=seal,
        )

        self.assertEqual(await worker.run_once(), len(bindings))
        self.assertEqual(len(sealed), len(bindings))

        for binding, remote_status in zip(bindings, expected, strict=True):
            call_status, safe_error_code = expected[remote_status]
            stored = await self._get(binding)
            self.assertEqual(stored.last_status, call_status)
            call = await self.storage.get_mcp_call_record(
                binding.owner_user_id, binding.task_id, binding.call_ref
            )
            self.assertEqual((call.status, call.safe_error_code), expected[remote_status])
            self.assertIsNone(call.result_ref)
            serialized = repr((stored, call))
            self.assertNotIn("raw-result", serialized)
            self.assertNotIn("raw-error", serialized)
            self.assertNotIn("vendor_secret_status", serialized)

    async def test_result_processor_can_turn_completed_remote_task_into_tool_error(self) -> None:
        binding = self._binding("parsed-tool-error")
        await self._reserve_call(binding)
        await self.storage.save_mcp_remote_task_binding(binding)
        client = _RecordingClient(
            state=MCPTaskState(
                safe_remote_task_ref=binding.safe_remote_task_ref,
                status="completed",
                terminal=True,
                result={
                    "resultType": "complete",
                    "content": [],
                    "isError": True,
                },
            )
        )
        processed_sources: list[str] = []
        sealed: list[tuple[str, str | None]] = []

        async def process(_binding, result, source):
            self.assertTrue(result["isError"])
            processed_sources.append(source)
            return MCPRemoteTaskProcessedResult(
                "failed", "mcp_tool_error", None
            )

        async def seal(_binding, call_status, _result_ref, safe_error_code):
            sealed.append((call_status, safe_error_code))

        worker = MCPRemoteTaskRecoveryWorker(
            storage=self.storage,
            client_factory=lambda _binding: client,
            instance_id="worker-parsed-tool-error",
            result_processor=process,
            terminal_sealer=seal,
            now_fn=lambda: self.now,
        )

        self.assertEqual(await worker.run_once(), 1)
        self.assertEqual(processed_sources, ["tasks_get"])
        self.assertEqual(sealed, [("failed", "mcp_tool_error")])
        call = await self.storage.get_mcp_call_record(
            binding.owner_user_id, binding.task_id, binding.call_ref
        )
        self.assertEqual(call.status, "failed")
        self.assertEqual(call.safe_error_code, "mcp_tool_error")
    async def test_terminal_metrics_follow_successful_atomic_convergence(self) -> None:
        expected = {
            "completed": (
                MCPMetricResultCategory.SUCCEEDED,
                MCPMetricErrorCategory.NONE,
            ),
            "failed": (
                MCPMetricResultCategory.FAILED,
                MCPMetricErrorCategory.SERVER,
            ),
            "cancelled": (
                MCPMetricResultCategory.CANCELLED,
                MCPMetricErrorCategory.NONE,
            ),
            "vendor_terminal": (
                MCPMetricResultCategory.UNKNOWN,
                MCPMetricErrorCategory.UNKNOWN,
            ),
        }
        terminal_at = self.now + timedelta(seconds=4)
        bindings = [
            self._binding(f"metric-{index}") for index, _ in enumerate(expected)
        ]
        for binding in bindings:
            await self._reserve_call(binding)
            await self.storage.save_mcp_remote_task_binding(binding)
        clients = {
            binding.safe_remote_task_ref: _RecordingClient(
                state=MCPTaskState(
                    safe_remote_task_ref=binding.safe_remote_task_ref,
                    status=remote_status,
                    terminal=True,
                )
            )
            for binding, remote_status in zip(bindings, expected, strict=True)
        }
        samples: list[MCPRemoteTaskTerminalMetricSample] = []

        async def record(sample: MCPRemoteTaskTerminalMetricSample) -> None:
            stored = await self._get(sample.binding)
            self.assertEqual(stored.terminal_at, terminal_at)
            samples.append(sample)

        worker = MCPRemoteTaskRecoveryWorker(
            storage=self.storage,
            client_factory=lambda binding: clients[binding.safe_remote_task_ref],
            instance_id="worker-terminal-metrics",
            now_fn=lambda: terminal_at,
            terminal_metric_sink=record,
            result_persister=lambda binding, _result: f"mcp-result-{binding.call_ref}",
        )

        self.assertEqual(await worker.run_once(), len(bindings))
        self.assertEqual(len(samples), len(bindings))
        by_ref = {sample.binding.safe_remote_task_ref: sample for sample in samples}
        for binding, remote_status in zip(bindings, expected, strict=True):
            sample = by_ref[binding.safe_remote_task_ref]
            self.assertEqual(
                (sample.result_category, sample.error_category),
                expected[remote_status],
            )
            self.assertEqual(sample.duration_seconds, 4.0)
            self.assertEqual(sample.terminal_at, terminal_at)

    async def test_metric_failure_does_not_retry_terminal_remote_task(self) -> None:
        binding = self._binding("metric-gap")
        await self._reserve_call(binding)
        await self.storage.save_mcp_remote_task_binding(binding)
        client = _RecordingClient(
            state=MCPTaskState(
                safe_remote_task_ref=binding.safe_remote_task_ref,
                status="completed",
                terminal=True,
            )
        )
        gaps: list[tuple[str, str]] = []

        async def fail_metric(_sample: MCPRemoteTaskTerminalMetricSample) -> None:
            raise RuntimeError("metric storage unavailable")

        async def record_gap(
            updated: MCPRemoteTaskBinding, reason: str
        ) -> None:
            gaps.append((updated.safe_remote_task_ref, reason))

        worker = MCPRemoteTaskRecoveryWorker(
            storage=self.storage,
            client_factory=lambda _binding: client,
            instance_id="worker-metric-gap",
            now_fn=lambda: self.now,
            terminal_metric_sink=fail_metric,
            metric_gap_sink=record_gap,
            result_persister=lambda _binding, _result: "mcp-result-metric-gap",
        )

        self.assertEqual(await worker.run_once(), 1)
        stored = await self._get(binding)
        self.assertEqual(stored.last_status, "completed")
        self.assertEqual(stored.terminal_at, self.now)
        self.assertIsNone(stored.next_poll_at)
        self.assertEqual(
            gaps,
            [(binding.safe_remote_task_ref, "terminal_recording_failed")],
        )
        self.assertEqual(await worker.run_once(), 0)

    async def test_terminal_metric_gap_failure_is_not_silently_swallowed(self) -> None:
        binding = self._binding("metric-gap-failure")
        await self._reserve_call(binding)
        await self.storage.save_mcp_remote_task_binding(binding)
        client = _RecordingClient(
            state=MCPTaskState(
                safe_remote_task_ref=binding.safe_remote_task_ref,
                status="completed",
                terminal=True,
            )
        )

        async def fail_metric(_sample: MCPRemoteTaskTerminalMetricSample) -> None:
            raise RuntimeError("metric storage unavailable")

        async def fail_gap(_binding, _reason: str) -> None:
            raise RuntimeError("audit storage unavailable")

        worker = MCPRemoteTaskRecoveryWorker(
            storage=self.storage,
            client_factory=lambda _binding: client,
            instance_id="worker-metric-gap-failure",
            now_fn=lambda: self.now,
            terminal_metric_sink=fail_metric,
            metric_gap_sink=fail_gap,
            result_persister=lambda _binding, _result: "mcp-result-gap-failure",
        )

        with self.assertRaises(MCPRemoteTaskRecoveryError):
            await worker.run_once()
        stored = await self._get(binding)
        self.assertEqual(stored.last_status, "completed")
        self.assertIsNotNone(stored.terminal_at)

    async def test_active_gauge_reconciles_all_exit_paths_and_gap_is_safe(self) -> None:
        binding = self._binding("active-gauge")
        await self._reserve_call(binding)
        await self.storage.save_mcp_remote_task_binding(binding)
        client = _RecordingClient(
            state=MCPTaskState(
                safe_remote_task_ref=binding.safe_remote_task_ref,
                status="input_required",
                terminal=False,
                input_requests={"approval": {"type": "boolean"}},
            )
        )
        samples: list[str] = []

        async def sample_active() -> None:
            samples.append("sampled")

        worker = MCPRemoteTaskRecoveryWorker(
            storage=self.storage,
            client_factory=lambda _binding: client,
            instance_id="worker-active-gauge",
            now_fn=lambda: self.now,
            active_metric_sink=sample_active,
        )

        self.assertEqual(await worker.run_once(), 1)
        self.assertEqual(await worker.run_once(), 0)
        await worker.aclose()
        self.assertEqual(samples, ["sampled", "sampled", "sampled"])
        stored = await self._get(binding)
        self.assertEqual(stored.last_status, "input_required")
        self.assertIsNone(stored.next_poll_at)
        self.assertIsNone(stored.terminal_at)

        gaps: list[str] = []

        async def fail_active() -> None:
            raise RuntimeError("metric storage unavailable")

        failing_worker = MCPRemoteTaskRecoveryWorker(
            storage=self.storage,
            client_factory=lambda _binding: client,
            instance_id="worker-active-gauge-gap",
            now_fn=lambda: self.now,
            active_metric_sink=fail_active,
            global_metric_gap_sink=gaps.append,
        )
        self.assertEqual(await failing_worker.run_once(), 0)
        self.assertEqual(gaps, ["active_gauge_recording_failed"])

    async def test_claim_loss_is_observable(self) -> None:
        binding = self._binding("claim-loss")
        await self.storage.save_mcp_remote_task_binding(binding)
        client = _RecordingClient(
            state=MCPTaskState(
                safe_remote_task_ref=binding.safe_remote_task_ref,
                status="completed",
                terminal=True,
            )
        )
        original_finish = self.storage.finish_mcp_remote_task_binding
        active_samples: list[str] = []

        async def lose_claim(*args: Any, **kwargs: Any) -> None:
            return None

        self.storage.finish_mcp_remote_task_binding = lose_claim  # type: ignore[method-assign]
        worker = MCPRemoteTaskRecoveryWorker(
            storage=self.storage,
            client_factory=lambda _binding: client,
            instance_id="worker-claim-loss",
            now_fn=lambda: self.now,
            active_metric_sink=lambda: active_samples.append("sampled"),
            result_persister=lambda _binding, _result: "mcp-result-claim-loss",
        )
        try:
            with self.assertRaisesRegex(RuntimeError, "mcp_remote_task_claim_lost"):
                await worker.run_once()
            self.assertEqual(active_samples, ["sampled"])
        finally:
            self.storage.finish_mcp_remote_task_binding = original_finish  # type: ignore[method-assign]

    async def test_claim_renew_exception_is_observable(self) -> None:
        binding = self._binding("claim-renew-error")
        await self.storage.save_mcp_remote_task_binding(binding)
        entered = asyncio.Event()
        release = asyncio.Event()
        client = _RecordingClient(
            state=MCPTaskState(
                safe_remote_task_ref=binding.safe_remote_task_ref,
                status="working",
                terminal=False,
            ),
            entered=entered,
            release=release,
        )
        original_renew = self.storage.renew_mcp_remote_task_binding_claim

        async def fail_renew(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("database unavailable")

        self.storage.renew_mcp_remote_task_binding_claim = fail_renew  # type: ignore[method-assign]
        worker = MCPRemoteTaskRecoveryWorker(
            storage=self.storage,
            client_factory=lambda _binding: client,
            instance_id="worker-claim-renew-error",
            now_fn=lambda: self.now,
            claim_ttl_seconds=0.2,
            claim_renew_seconds=0.01,
        )
        try:
            run = asyncio.create_task(worker.run_once())
            await asyncio.wait_for(entered.wait(), timeout=1)
            await asyncio.sleep(0.02)
            release.set()
            with self.assertRaisesRegex(
                RuntimeError, "mcp_remote_task_claim_renew_failed"
            ):
                await run
        finally:
            self.storage.renew_mcp_remote_task_binding_claim = original_renew  # type: ignore[method-assign]

    async def test_poll_error_and_unsupported_protocol_back_off_fail_closed(
        self,
    ) -> None:
        failed = self._binding("failed")
        unsupported = self._binding("unsupported", protocol_version="2025-06-18")
        await self.storage.save_mcp_remote_task_binding(failed)
        await self.storage.save_mcp_remote_task_binding(unsupported)
        failed_client = _RecordingClient(error=RuntimeError("remote unavailable"))
        unsupported_factory_calls = 0

        def client_factory(binding: MCPRemoteTaskBinding) -> _RecordingClient:
            nonlocal unsupported_factory_calls
            if binding.safe_remote_task_ref == unsupported.safe_remote_task_ref:
                unsupported_factory_calls += 1
                raise AssertionError("unsupported protocols must not create a client")
            return failed_client

        worker = MCPRemoteTaskRecoveryWorker(
            storage=self.storage,
            client_factory=client_factory,
            instance_id="worker-errors",
            now_fn=lambda: self.now,
            error_backoff_seconds=17,
        )

        self.assertEqual(await worker.run_once(), 2)

        for original in (failed, unsupported):
            stored = await self._get(original)
            self.assertEqual(stored.last_status, "working")
            self.assertEqual(stored.next_poll_at, self.now + timedelta(seconds=17))
            self.assertIsNone(stored.claim_owner)
        self.assertEqual(unsupported_factory_calls, 0)
        self.assertEqual(failed_client.calls[0][0], "tasks/get")
        self.assertEqual(failed_client.calls[-1], "aclose")

    async def test_2025_input_required_closes_unsupported_without_retry(self) -> None:
        binding = self._binding(
            "2025-input-required", protocol_version="2025-11-25"
        )
        await self._reserve_call(binding)
        await self.storage.save_mcp_remote_task_binding(binding)

        class _Client2025InputRequired:
            async def tasks_get(self, safe_ref, *, recovery_context):
                del recovery_context
                return MCP2025TaskState(safe_ref, "input_required", False)

            async def aclose(self):
                return None

        worker = MCPRemoteTaskRecoveryWorker(
            storage=self.storage,
            client_factory=lambda _binding: _Client2025InputRequired(),
            instance_id="worker-2025-input-required",
            now_fn=lambda: self.now,
        )

        self.assertEqual(await worker.run_once(), 1)
        stored = await self._get(binding)
        self.assertEqual(stored.last_status, "input_required")
        self.assertIsNotNone(stored.terminal_at)
        call = await self.storage.get_mcp_call_record(
            binding.owner_user_id, binding.task_id, binding.call_ref
        )
        self.assertEqual(call.status, "failed")
        self.assertEqual(
            call.safe_error_code,
            "mcp_remote_task_input_required_unsupported",
        )
        self.assertEqual(await worker.run_once(), 0)

    async def test_run_forever_recovers_after_one_iteration_failure(self) -> None:
        gaps: list[str] = []
        worker = MCPRemoteTaskRecoveryWorker(
            storage=self.storage,
            client_factory=lambda _binding: None,
            instance_id="worker-loop-recovery",
            idle_poll_seconds=0.01,
            error_backoff_seconds=0.01,
            global_metric_gap_sink=gaps.append,
        )
        attempts = 0

        async def flaky_run_once():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("injected recoverable iteration failure")
            worker._stop.set()
            return 0

        worker.run_once = flaky_run_once
        await asyncio.wait_for(worker.run_forever(), timeout=1)

        self.assertEqual(attempts, 2)
        self.assertEqual(gaps, ["mcp_remote_task_worker_run_failed"])

    async def test_aclose_cancels_inflight_poll_and_releases_claim(self) -> None:
        binding = self._binding("shutdown")
        await self.storage.save_mcp_remote_task_binding(binding)
        entered = asyncio.Event()
        never_release = asyncio.Event()
        client = _RecordingClient(
            state=MCPTaskState(
                safe_remote_task_ref=binding.safe_remote_task_ref,
                status="working",
                terminal=False,
            ),
            entered=entered,
            release=never_release,
        )
        worker = MCPRemoteTaskRecoveryWorker(
            storage=self.storage,
            client_factory=lambda _binding: client,
            instance_id="worker-shutdown",
            now_fn=lambda: self.now,
            claim_ttl_seconds=0.2,
            claim_renew_seconds=0.01,
        )

        await worker.start()
        await asyncio.wait_for(entered.wait(), timeout=1)
        await worker.aclose()

        stored = await self._get(binding)
        self.assertIsNone(stored.claim_owner)
        self.assertEqual(stored.next_poll_at, self.now)
        self.assertEqual(client.calls[-1], "aclose")


if __name__ == "__main__":
    unittest.main()
