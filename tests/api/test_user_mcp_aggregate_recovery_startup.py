from __future__ import annotations

import asyncio
import inspect
import os
import tempfile
from functools import partial
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.api.runtime import ApiRuntime, build_api_runtime as _build_api_runtime
from src.core.enums import NodeStatus, TaskStatus
from src.core.models import MCPTerminalState
from tests.api.support import InMemoryTaskRuntimeSidecar

build_api_runtime = partial(
    _build_api_runtime,
    skill_roots=(),
    public_skill_roots=(),
)


class UserMCPAggregateRecoveryStartupTest(unittest.IsolatedAsyncioTestCase):
    def test_runtime_startup_and_shutdown_authority_order_is_exact(self) -> None:
        startup_source = inspect.getsource(ApiRuntime.start)
        startup_markers = (
            "create_or_verify_sentinel",
            "_admit_mcp_rollout_instance",
            "await aggregate_reconciler.run()",
            "await self._reconcile_mcp_dispatch_recovery()",
            "await self._recover_agent_runs()",
            "self._mcp_post_ready_recovery_task = asyncio.create_task",
            "self._mcp_result_artifact_reconciler_task = asyncio.create_task",
            "self._mcp_disk_cleanup_task = asyncio.create_task",
        )
        self.assertEqual(
            [startup_source.count(marker) for marker in startup_markers],
            [1, 1, 1, 1, 1, 1, 1, 1],
        )
        startup_positions = [startup_source.index(marker) for marker in startup_markers]
        self.assertEqual(startup_positions, sorted(startup_positions))

        shutdown_source = inspect.getsource(ApiRuntime.shutdown)
        shutdown_markers = (
            "await self._quiesce_cp7_for_shutdown()",
            "self._mcp_disk_cleanup_task.cancel()",
            "self._mcp_result_artifact_reconciler_task.cancel()",
            "self._mcp_post_ready_recovery_task.cancel()",
            "await self._close_cp7_safety()",
            "await self.user_mcp_config_service.aclose()",
            "self._engine.dispose()",
        )
        self.assertEqual(
            [shutdown_source.count(marker) for marker in shutdown_markers],
            [1, 1, 1, 1, 1, 1, 1],
        )
        shutdown_positions = [shutdown_source.index(marker) for marker in shutdown_markers]
        self.assertEqual(shutdown_positions, sorted(shutdown_positions))

    async def test_mcp_disk_cleanup_cycle_is_fail_open_and_uses_aware_time(
        self,
    ) -> None:
        runtime = object.__new__(ApiRuntime)
        runtime.user_mcp_result_janitor = AsyncMock(
            side_effect=RuntimeError("temporary cleanup failed")
        )
        runtime.user_mcp_result_store = Mock()
        runtime.user_mcp_result_store.active_task_keys.return_value = {
            "task-active"
        }
        runtime._mcp_pending_action_payload_store = AsyncMock()
        runtime.storage = AsyncMock()
        runtime.storage.list_protected_mcp_pending_action_payload_refs.return_value = (
            "protected-ref",
        )

        await runtime._run_mcp_disk_cleanup_once()

        runtime.user_mcp_result_janitor.cleanup_orphans.assert_awaited_once_with(
            active_task_keys={"task-active"}
        )
        runtime._mcp_pending_action_payload_store.cleanup_orphans.assert_awaited_once()
        cleanup_call = (
            runtime._mcp_pending_action_payload_store.cleanup_orphans.await_args
        )
        self.assertEqual(
            cleanup_call.kwargs["referenced_payload_refs"],
            ("protected-ref",),
        )
        self.assertIsNotNone(cleanup_call.kwargs["now"].utcoffset())

        runtime.user_mcp_result_janitor.cleanup_orphans.side_effect = None
        runtime._mcp_pending_action_payload_store.cleanup_orphans.reset_mock()
        runtime.storage.list_protected_mcp_pending_action_payload_refs.side_effect = (
            RuntimeError("protection query failed")
        )

        await runtime._run_mcp_disk_cleanup_once()

        runtime.user_mcp_result_janitor.cleanup_orphans.assert_awaited()
        runtime._mcp_pending_action_payload_store.cleanup_orphans.assert_not_awaited()

    async def test_mcp_disk_cleanup_loop_runs_before_hourly_sleep(self) -> None:
        runtime = object.__new__(ApiRuntime)
        runtime._run_mcp_disk_cleanup_once = AsyncMock()
        delays: list[int] = []

        async def sleep(delay: int) -> None:
            delays.append(delay)
            raise asyncio.CancelledError

        runtime._mcp_disk_cleanup_sleep = sleep

        with self.assertRaises(asyncio.CancelledError):
            await runtime._run_mcp_disk_cleanup_forever()

        runtime._run_mcp_disk_cleanup_once.assert_awaited_once_with()
        self.assertEqual(delays, [3600])

    async def test_partial_startup_and_first_shutdown_error_keep_current_cleanup_gap(self) -> None:
        runtime = object.__new__(ApiRuntime)
        runtime.storage = object()
        runtime._engine = Mock()
        runtime._master_key_sentinel_cipher = AsyncMock()
        runtime.recover_deleting_conversations = AsyncMock()
        runtime._admit_mcp_rollout_instance = AsyncMock(
            side_effect=RuntimeError("partial-startup-failure")
        )

        with self.assertRaisesRegex(RuntimeError, "partial-startup-failure"):
            await runtime.start()
        runtime._engine.dispose.assert_not_called()

        runtime._quiesce_cp7_for_shutdown = AsyncMock(
            side_effect=RuntimeError("first-shutdown-failure")
        )
        with self.assertRaisesRegex(RuntimeError, "first-shutdown-failure"):
            await runtime.shutdown()
        runtime._engine.dispose.assert_not_called()

    @staticmethod
    def _runtime(storage: AsyncMock) -> ApiRuntime:
        runtime = object.__new__(ApiRuntime)
        runtime.storage = storage
        runtime._utcnow_naive = lambda: datetime(2026, 8, 19, 12, 0, 0)
        return runtime

    @staticmethod
    def _built_runtime(root: Path) -> ApiRuntime:
        return build_api_runtime(
            database_path=root / "runtime.sqlite3",
            audit_log_path=root / "audit.jsonl",
            enable_user_mcp=True,
            master_key_bytes=b"a" * 32,
            enable_platform_llm=False,
            enable_conversation_title_llm=False,
            enable_conversation_memory=False,
            runtime_sidecar_client=InMemoryTaskRuntimeSidecar(),
        )

    async def test_dispatch_recovery_finishes_before_runtime_becomes_ready(self) -> None:
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
            runtime = self._built_runtime(Path(directory))
            started = asyncio.Event()
            release = asyncio.Event()

            async def startup_recovery() -> None:
                started.set()
                await release.wait()

            runtime._reconcile_mcp_dispatch_recovery = startup_recovery

            start_task = asyncio.create_task(runtime.start())
            await asyncio.wait_for(started.wait(), timeout=1)
            self.assertFalse(start_task.done())
            self.assertIsNone(runtime._mcp_result_artifact_reconciler_task)
            self.assertIsNone(runtime._mcp_disk_cleanup_task)

            release.set()
            await asyncio.wait_for(start_task, timeout=2)
            self.assertIsNotNone(runtime._mcp_result_artifact_reconciler_task)
            self.assertFalse(runtime._mcp_result_artifact_reconciler_task.done())
            self.assertIsNotNone(runtime._mcp_disk_cleanup_task)
            self.assertFalse(runtime._mcp_disk_cleanup_task.done())

            await runtime.shutdown()
            self.assertIsNone(runtime._mcp_result_artifact_reconciler_task)
            self.assertIsNone(runtime._mcp_disk_cleanup_task)

    async def test_result_artifact_reconciler_runs_serial_cycles_every_sixty_seconds(
        self,
    ) -> None:
        runtime = object.__new__(ApiRuntime)
        manager = AsyncMock()
        second_cycle_started = asyncio.Event()
        hold_second_cycle = asyncio.Event()
        cycle_count = 0

        async def reconcile(*, limit: int):
            nonlocal cycle_count
            self.assertEqual(limit, 1000)
            cycle_count += 1
            if cycle_count == 2:
                second_cycle_started.set()
                await hold_second_cycle.wait()

        delays: list[int] = []

        async def sleep(delay: int) -> None:
            delays.append(delay)

        manager.reconcile_artifacts_and_gc_once.side_effect = reconcile
        runtime._mcp_durable_result_lifecycle_manager = manager
        runtime._mcp_result_artifact_projector = object()
        runtime._mcp_result_artifact_reconciler_sleep = sleep
        runtime._audit_sink = None

        task = asyncio.create_task(
            runtime._run_mcp_result_artifact_reconciler_forever()
        )
        await asyncio.wait_for(second_cycle_started.wait(), timeout=1)

        self.assertEqual(cycle_count, 2)
        self.assertEqual(delays, [60])
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_pre_ready_terminal_recovery_only_reconciles_untracked_results(
        self,
    ) -> None:
        runtime = object.__new__(ApiRuntime)
        manager = AsyncMock()
        manager.reconcile_untracked.return_value = (0, 0)
        runtime._mcp_startup_terminal_candidates = ()
        runtime._mcp_durable_result_lifecycle_manager = manager

        await runtime._reconcile_mcp_terminal_candidates()

        manager.reconcile_untracked.assert_awaited_once_with(limit=1000)
        manager.run_once.assert_not_awaited()
        manager.reconcile_artifacts_and_gc_once.assert_not_awaited()

    async def test_terminal_candidate_recovery_retries_pending_payload_delete(
        self,
    ) -> None:
        runtime = object.__new__(ApiRuntime)
        candidate = SimpleNamespace(
            terminal_state=MCPTerminalState.FAILED,
            owner_user_id="alice",
            task_id="task-1",
            node_id="node-1",
            call_id="call-1",
            candidate_id="candidate-1",
            result_payload_sha256="sha256:" + "1" * 64,
        )
        item = SimpleNamespace(candidate=candidate)
        candidate_snapshot = object()
        call = SimpleNamespace(call_ref="call-1")
        runtime._mcp_startup_terminal_candidates = (item,)
        runtime._mcp_terminal_candidate_snapshot_authority = Mock()
        runtime._mcp_terminal_candidate_snapshot_authority.snapshot.return_value = (
            candidate_snapshot
        )
        runtime._mcp_durable_result_snapshot_authority = None
        runtime._mcp_pending_action_payload_store = object()
        runtime._mcp_durable_result_lifecycle_manager = None
        runtime._utcnow_naive = lambda: datetime(2026, 8, 19, 12, 0, 0)
        runtime.storage = AsyncMock()
        runtime.storage.recover_mcp_terminal_candidate.return_value = "committed_normal"
        runtime.storage.get_mcp_call_record.return_value = call
        runtime._delete_mcp_pending_action_payload_best_effort = AsyncMock()

        await runtime._reconcile_mcp_terminal_candidates()

        runtime.storage.recover_mcp_terminal_candidate.assert_awaited_once()
        runtime.storage.get_mcp_call_record.assert_awaited_once_with(
            "alice", "task-1", "call-1"
        )
        runtime._delete_mcp_pending_action_payload_best_effort.assert_awaited_once_with(
            call
        )

    async def test_candidate_capacity_warning_triggers_immediate_archive_scan(
        self,
    ) -> None:
        runtime = object.__new__(ApiRuntime)
        runtime._mcp_terminal_result_root = Path("/unused-in-patched-test")
        runtime._mcp_startup_terminal_candidates = None
        runtime._audit_sink = AsyncMock()
        runtime._mcp_terminal_candidate_lifecycle_manager = AsyncMock()
        runtime._mcp_terminal_candidate_lifecycle_manager.run_once.return_value = (
            0,
            0,
            0,
        )
        candidates = tuple(object() for _ in range(8_000))

        with patch(
            "src.api.runtime.enumerate_unconsumed_terminal_result_candidates",
            return_value=candidates,
        ):
            await runtime._strict_enumerate_mcp_terminal_candidates()

        runtime._audit_sink.record.assert_awaited_once_with(
            "mcp.terminal_candidate_capacity_warning",
            {
                "active_candidate_count": 8_000,
                "status": "warning",
            },
        )
        runtime._mcp_terminal_candidate_lifecycle_manager.run_once.assert_awaited_once_with(
            limit=1000
        )
        self.assertIs(runtime._mcp_startup_terminal_candidates, candidates)

    async def test_startup_classifies_untracked_durable_result_as_orphan(
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
            runtime = self._built_runtime(Path(directory))
            sink = runtime.user_mcp_result_store.create_sink(
                "orphan-task",
                scope_id="orphan-scope",
                durable=True,
                owner_user_id="alice",
                node_id="orphan-node",
                call_ref="orphan-call",
            )
            await sink.write(b'{"orphan":true}')
            result = await sink.finalize()

            await runtime.start()
            lifecycle = (
                await runtime.storage.get_mcp_durable_result_lifecycle(
                    result.ref
                )
            )

            self.assertEqual(str(lifecycle.status), "retained")
            self.assertEqual(str(lifecycle.reason), "orphan")
            self.assertIsNotNone(lifecycle.eligible_at)
            self.assertEqual(
                runtime.user_mcp_result_store.resolve_ref(result.ref).ref,
                result.ref,
            )
            await runtime.shutdown()

    async def test_waiting_input_without_open_mrtr_interrupt_blocks_ready(self) -> None:
        storage = AsyncMock()
        storage.list_mcp_dispatch_resume_outboxes.return_value = [
            SimpleNamespace(
                status="waiting_input",
                task_id="task-1",
                node_id="node-1",
                owner_user_id="alice",
            )
        ]
        storage.get_task.return_value = SimpleNamespace(
            status=TaskStatus.RUNNING,
            cancel_requested_at=None,
        )
        storage.get_task_node.return_value = SimpleNamespace(
            status=NodeStatus.WAITING_FOR_INPUT
        )
        storage.list_interrupts_for_task.return_value = []
        runtime = self._runtime(storage)

        with self.assertRaisesRegex(
            RuntimeError, "mcp_startup_mrtr_interrupt_authority_invalid"
        ):
            await runtime._validate_mcp_mrtr_recovery_evidence()

    async def test_waiting_approval_without_pending_action_blocks_ready(self) -> None:
        storage = AsyncMock()
        storage.list_mcp_dispatch_resume_outboxes.return_value = [
            SimpleNamespace(
                status="waiting_approval",
                task_id="task-1",
                node_id="node-1",
                owner_user_id="alice",
                server_id="server-1",
            )
        ]
        storage.get_task.return_value = SimpleNamespace(
            status=TaskStatus.RUNNING,
            cancel_requested_at=None,
        )
        storage.get_task_node.return_value = SimpleNamespace(
            status=NodeStatus.WAITING_FOR_INPUT
        )
        storage.list_interrupts_for_task.return_value = [
            SimpleNamespace(
                interrupt_id="interrupt-1",
                node_id="node-1",
                reason_code="mcp_tool_approval_required",
                status="open",
            )
        ]
        storage.get_mcp_pending_tool_action_for_interrupt.return_value = None
        runtime = self._runtime(storage)

        with self.assertRaisesRegex(
            RuntimeError, "mcp_startup_pending_action_authority_invalid"
        ):
            await runtime._validate_mcp_pending_action_recovery_evidence()

    async def test_available_intent_with_digest_mismatch_blocks_ready(self) -> None:
        storage = AsyncMock()
        storage.get_mcp_dispatch_resume_outbox.return_value = SimpleNamespace(
            intent_id="intent-1",
            outbox_id="mcp-dispatch-resume-v1:intent-1",
            resume_envelope_sha256="wrong",
            payload_sha256="wrong",
        )
        storage.list_mcp_no_server_intents.return_value = [
            SimpleNamespace(
                intent_id="intent-1",
                status="available",
                resume_envelope_json={"schema": "invalid"},
                resume_envelope_sha256="wrong",
                node_id="node-1",
                owner_user_id="alice",
                requested_server_id="server-1",
                task_id="task-1",
            )
        ]
        runtime = self._runtime(storage)

        with self.assertRaisesRegex(
            RuntimeError, "mcp_startup_resume_envelope_authority_invalid"
        ):
            await runtime._validate_mcp_resume_envelope_authority()

    async def test_expired_claim_is_recovered_before_unknown_convergence(self) -> None:
        now = datetime(2026, 8, 19, 12, 0, 0)
        outbox = SimpleNamespace(
            outbox_id="outbox-1",
            status="claimed",
            lease_expires_at=now - timedelta(seconds=1),
            revision=3,
        )
        storage = AsyncMock()
        storage.list_mcp_dispatch_resume_outboxes.return_value = [outbox]
        storage.release_or_recover_mcp_dispatch_claim.return_value = SimpleNamespace(
            status="pending"
        )
        runtime = self._runtime(storage)

        await runtime._recover_expired_mcp_dispatch_claims()

        storage.release_or_recover_mcp_dispatch_claim.assert_awaited_once_with(
            "outbox-1", 3, now
        )

    async def test_inactive_task_dispatch_converges_before_final_invariants(
        self,
    ) -> None:
        storage = AsyncMock()
        storage.list_mcp_no_server_intents.return_value = [
            SimpleNamespace(
                intent_id="intent-1",
                status="available",
                owner_user_id="alice",
                task_id="task-1",
                node_id="node-1",
                updated_at=datetime(2026, 8, 19, 11, 0, 0),
            )
        ]
        storage.get_task.return_value = SimpleNamespace(
            status=TaskStatus.FAILED,
            cancel_requested_at=None,
        )
        storage.converge_inactive_mcp_dispatch.return_value = "finalized"
        storage.converge_dispatched_mcp_calls_to_unknown.return_value = []
        same_node_call = SimpleNamespace(call_ref="call-2", node_id="node-1")
        other_node_call = SimpleNamespace(call_ref="call-1", node_id="node-2")
        storage.list_mcp_call_records.return_value = [
            same_node_call,
            other_node_call,
        ]
        runtime = self._runtime(storage)
        runtime._mcp_pending_action_payload_store = object()
        runtime._delete_mcp_pending_action_payload_best_effort = AsyncMock()

        await runtime._converge_inactive_and_unknown_mcp_dispatches()

        storage.converge_inactive_mcp_dispatch.assert_awaited_once()
        storage.list_mcp_call_records.assert_awaited_once_with("alice", "task-1")
        runtime._delete_mcp_pending_action_payload_best_effort.assert_awaited_once_with(
            same_node_call
        )
        storage.converge_dispatched_mcp_calls_to_unknown.assert_awaited_once()

    async def test_incomplete_active_claim_shape_blocks_ready(self) -> None:
        storage = AsyncMock()
        storage.list_mcp_no_server_intents.return_value = []
        outbox = SimpleNamespace(
            status="active",
            task_id="task-1",
            claim_owner=None,
            claim_token=None,
            lease_expires_at=None,
        )
        storage.list_mcp_dispatch_resume_outboxes.return_value = [outbox]
        storage.get_task.return_value = SimpleNamespace(
            status=TaskStatus.RUNNING,
            cancel_requested_at=None,
        )
        runtime = self._runtime(storage)

        with self.assertRaisesRegex(
            RuntimeError, "mcp_startup_dispatch_claim_shape_invalid"
        ):
            await runtime._validate_mcp_aggregate_invariants()

    async def test_cancelled_no_call_accepts_cancelled_node_authority(self) -> None:
        storage = AsyncMock()
        intent = SimpleNamespace(
            intent_id="intent-1",
            owner_user_id="alice",
            task_id="task-1",
            node_id="node-1",
            status="resolved",
            terminal_at=datetime(2026, 8, 19, 11, 0, 0),
        )
        storage.get_task.return_value = SimpleNamespace(
            status=TaskStatus.CANCELLED
        )
        storage.get_task_node.return_value = SimpleNamespace(
            status=NodeStatus.CANCELLED
        )
        storage.list_events_for_task.return_value = [
            SimpleNamespace(
                event_id="mcp-dispatch-finalized:v1:intent-1:4",
                event_type="mcp.dispatch_finalized",
                payload={"completion_mode": "cancelled_no_call"},
            )
        ]
        storage.get_mcp_dispatch_resume_outbox.return_value = SimpleNamespace(
            status="aborted",
            completion_mode="cancelled_no_call",
            result_receipt_id=None,
        )
        storage.list_mcp_call_records.return_value = []
        runtime = self._runtime(storage)

        await runtime._validate_terminal_cp7_mcp_authority([intent])

    async def test_stopped_after_call_is_valid_resolved_restart_authority(
        self,
    ) -> None:
        storage = AsyncMock()
        intent = SimpleNamespace(
            intent_id="intent-stopped-after-call",
            owner_user_id="alice",
            task_id="task-stopped-after-call",
            node_id="node-stopped-after-call",
            status="resolved",
            terminal_at=datetime(2026, 8, 19, 11, 0, 0),
        )
        storage.get_task.return_value = SimpleNamespace(
            status=TaskStatus.COMPLETED
        )
        storage.get_task_node.return_value = SimpleNamespace(
            status=NodeStatus.COMPLETED
        )
        storage.list_events_for_task.return_value = []
        storage.get_mcp_dispatch_resume_outbox.return_value = SimpleNamespace(
            status="completed",
            completion_mode="stopped_after_call",
            result_receipt_id="receipt-1",
        )
        storage.list_mcp_call_records.return_value = [
            SimpleNamespace(call_ref="call-1", may_have_dispatched=True)
        ]
        storage.get_mcp_terminal_result_receipt_for_call.return_value = (
            SimpleNamespace(result_receipt_id="receipt-1")
        )
        runtime = self._runtime(storage)

        await runtime._validate_terminal_cp7_mcp_authority([intent])

    async def test_completed_no_call_is_valid_resolved_restart_authority(
        self,
    ) -> None:
        intent_id = "intent-completed-no-call"
        intent = SimpleNamespace(
            intent_id=intent_id,
            owner_user_id="alice",
            task_id="task-completed-no-call",
            node_id="node-completed-no-call",
            status="resolved",
            terminal_at=datetime(2026, 8, 19, 11, 0, 0),
        )
        storage = AsyncMock()
        storage.get_task.return_value = SimpleNamespace(
            status=TaskStatus.COMPLETED
        )
        storage.get_task_node.return_value = SimpleNamespace(
            status=NodeStatus.COMPLETED
        )
        storage.list_events_for_task.return_value = [
            SimpleNamespace(
                event_id=f"mcp-dispatch-finalized:v1:{intent_id}:1",
                event_type="mcp.dispatch_finalized",
                payload={"completion_mode": "completed"},
            )
        ]
        storage.get_mcp_dispatch_resume_outbox.return_value = SimpleNamespace(
            status="completed",
            completion_mode="completed",
            result_receipt_id=None,
        )
        storage.list_mcp_call_records.return_value = []
        runtime = self._runtime(storage)

        await runtime._validate_terminal_cp7_mcp_authority([intent])


if __name__ == "__main__":
    unittest.main()
