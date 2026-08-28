from __future__ import annotations

import asyncio
import inspect
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from src.api.runtime import ApiRuntime
from src.api.submission_admission import PreparedAgentRecoveryContext
from src.api.upload_errors import UploadValidationError
from src.core.enums import MessageRole, NodeStatus, TaskStatus
from src.core.models import (
    Conversation,
    ConversationFileResource,
    Message,
    Task,
    TaskInputAttachment,
    TaskNode,
)
from src.orchestration.agent_loop.models import (
    AgentItem,
    AgentItemKind,
    AgentItemState,
    AgentModelBinding,
    AgentRun,
    AgentRunStatus,
    AgentStorageConflict,
)
from src.orchestration.models import UserMCPServerProfile


class _StopStartup(RuntimeError):
    pass


class SubmissionAdmissionRuntimeStartupTest(unittest.IsolatedAsyncioTestCase):
    def test_skill_result_janitor_runs_after_agent_recovery_before_background_services(self) -> None:
        source = inspect.getsource(ApiRuntime.start)
        self.assertLess(
            source.index("await self._recover_agent_runs()"),
            source.index("await agent_transient_result_janitor.run_once()"),
        )
        self.assertLess(
            source.index("await agent_transient_result_janitor.run_once()"),
            source.index("await agent_skill_result_janitor.run_once()"),
        )
        self.assertLess(
            source.index("await agent_skill_result_janitor.run_once()"),
            source.index("if self._mcp_cp7_safety_facade is not None"),
        )

    async def test_startup_terminal_event_reconciliation_is_exact(self) -> None:
        run = AgentRun(
            "run-terminal-event",
            "task-terminal-event",
            "conv-terminal-event",
            AgentRunStatus.RUNNING,
            AgentModelBinding("edition-a"),
        )
        call = AgentItem(
            "call-terminal-event",
            run.run_id,
            run.task_id,
            1,
            AgentItemKind.TOOL_CALL,
            AgentItemState.COMMITTED,
            '{"capability_id":"skill.lookup","node_id":"node-terminal-event"}\n',
            "a" * 64,
        )
        result = AgentItem(
            "result-terminal-event",
            run.run_id,
            run.task_id,
            2,
            AgentItemKind.TOOL_RESULT,
            AgentItemState.COMMITTED,
            '{"outcome":"completed","safe_error_code":null}\n',
            "b" * 64,
            source_call_item_id=call.item_id,
            committed_at=datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc),
        )
        events = {}

        async def append_event_exact(event):
            existing = events.get(event.event_id)
            if existing is not None:
                return existing, True
            events[event.event_id] = event
            return event, False

        runtime = object.__new__(ApiRuntime)
        runtime.agent_run_repository = SimpleNamespace(
            list_items=AsyncMock(return_value=(call, result))
        )
        runtime.storage = SimpleNamespace(
            get_task_node=AsyncMock(
                return_value=TaskNode(
                    "node-terminal-event",
                    run.task_id,
                    "skill.lookup",
                    status=NodeStatus.COMPLETED,
                )
            ),
            append_event_exact=append_event_exact,
        )
        runtime.event_broker = SimpleNamespace(publish=AsyncMock())

        await runtime._reconcile_agent_terminal_events(run)
        await runtime._reconcile_agent_terminal_events(run)

        self.assertEqual(len(events), 1)
        event = next(iter(events.values()))
        self.assertEqual(event.event_type, "node.completed")
        self.assertEqual(event.payload["result_sha256"], result.payload_sha256)
        runtime.event_broker.publish.assert_awaited_once_with(event)

    @staticmethod
    def _startup_runtime(events: list[str], coordinator: object) -> ApiRuntime:
        runtime = object.__new__(ApiRuntime)
        runtime.storage = object()
        runtime._engine = Mock()
        runtime._submission_admission_coordinator = coordinator
        runtime._expected_submission_authority_receipt_sha256 = None
        runtime._runtime_sidecar_client = SimpleNamespace(
            claim_pending_submission=AsyncMock()
        )

        async def record(name: str) -> None:
            events.append(name)

        runtime._master_key_sentinel_cipher = SimpleNamespace(
            create_or_verify_sentinel=lambda _storage: record("sentinel")
        )
        runtime.recover_deleting_conversations = lambda: record(
            "deleting-conversations-recovered"
        )
        runtime._admit_mcp_rollout_instance = lambda: record("mcp-admit")
        for name in (
            "_repair_mcp_terminal_candidate_lifecycle",
            "_strict_enumerate_mcp_terminal_candidates",
            "_reconcile_mcp_terminal_candidates",
            "_reconcile_mcp_remote_bindings",
            "_validate_mcp_mrtr_recovery_evidence",
            "_validate_mcp_pending_action_recovery_evidence",
            "_validate_mcp_resume_envelope_authority",
            "_recover_expired_mcp_dispatch_claims",
            "_converge_inactive_and_unknown_mcp_dispatches",
            "_validate_mcp_aggregate_invariants",
        ):
            setattr(runtime, name, lambda marker=name: record(marker))
        runtime._reconcile_mcp_dispatch_recovery = lambda: record("mcp-reconciled")

        async def stop_after_agent_recovery_boundary() -> None:
            events.append("agent-recovery")
            raise _StopStartup("startup-boundary-reached")

        runtime._recover_agent_runs = stop_after_agent_recovery_boundary
        return runtime

    @staticmethod
    def _authority_probe_response(receipt: str) -> dict[str, object]:
        return {
            "operation": "submission_pending_claim",
            "found": False,
            "admission": None,
            "claim": None,
            "authority_state": "finalized",
            "finalization_receipt_sha256": receipt,
            "error": None,
            "pending_count": 0,
            "earliest_claim_expires_at_ms": None,
        }

    async def test_authority_probe_runs_between_sentinel_and_delete_recovery(self) -> None:
        events: list[str] = []
        receipt = "7" * 64
        runtime = self._startup_runtime(events, None)
        runtime._expected_submission_authority_receipt_sha256 = receipt

        async def claim_pending_submission(**_kwargs) -> dict[str, object]:
            events.append("authority-probe")
            return self._authority_probe_response(receipt)

        runtime._runtime_sidecar_client = SimpleNamespace(
            claim_pending_submission=AsyncMock(side_effect=claim_pending_submission)
        )

        with self.assertRaisesRegex(_StopStartup, "startup-boundary-reached"):
            await runtime.start()

        self.assertLess(events.index("sentinel"), events.index("authority-probe"))
        self.assertLess(
            events.index("authority-probe"),
            events.index("deleting-conversations-recovered"),
        )
        runtime._runtime_sidecar_client.claim_pending_submission.assert_awaited_once()
        probe = runtime._runtime_sidecar_client.claim_pending_submission.await_args.kwargs
        self.assertEqual(probe["after_created_at_ms"], 9_223_372_036_854_775_807)
        self.assertEqual(probe["after_message_id"], "")

    async def test_authority_receipt_mismatch_blocks_before_delete_recovery(self) -> None:
        events: list[str] = []
        runtime = self._startup_runtime(events, None)
        runtime._expected_submission_authority_receipt_sha256 = "7" * 64
        runtime._runtime_sidecar_client = SimpleNamespace(
            claim_pending_submission=AsyncMock(
                return_value=self._authority_probe_response("8" * 64)
            )
        )

        with self.assertRaisesRegex(
            RuntimeError, "submission_authority_receipt_mismatch"
        ):
            await runtime.start()

        self.assertEqual(events, ["sentinel"])

    async def test_authority_probe_rejects_nonempty_or_unfinalized_state(self) -> None:
        receipt = "7" * 64
        cases = {
            "pending": {
                **self._authority_probe_response(receipt),
                "pending_count": 1,
                "earliest_claim_expires_at_ms": 9_223_372_036_854_775_807,
            },
            "unfinalized": {
                **self._authority_probe_response(receipt),
                "authority_state": "uninitialized",
                "finalization_receipt_sha256": None,
            },
            "found": {
                **self._authority_probe_response(receipt),
                "found": True,
            },
        }
        for name, response in cases.items():
            with self.subTest(name=name):
                events: list[str] = []
                runtime = self._startup_runtime(events, None)
                runtime._expected_submission_authority_receipt_sha256 = receipt
                runtime._runtime_sidecar_client = SimpleNamespace(
                    claim_pending_submission=AsyncMock(return_value=response)
                )

                with self.assertRaises(RuntimeError):
                    await runtime.start()

                self.assertEqual(events, ["sentinel"])

    async def test_without_expected_receipt_skips_probe_and_reaches_agent_recovery(self) -> None:
        events: list[str] = []
        runtime = self._startup_runtime(events, None)

        with self.assertRaisesRegex(_StopStartup, "startup-boundary-reached"):
            await runtime.start()

        runtime._runtime_sidecar_client.claim_pending_submission.assert_not_awaited()
        self.assertIn("agent-recovery", events)

    async def test_projection_and_handoff_wrap_pre_agent_recovery_startup(self) -> None:
        events: list[str] = []

        class Coordinator:
            async def project_pending(self) -> None:
                events.append("submission-projected")

            async def recover_projected_handoffs(self) -> None:
                events.append("submission-handoff")

            async def abort_pending(self) -> None:
                events.append("submission-abort")

        runtime = self._startup_runtime(events, Coordinator())

        with self.assertRaisesRegex(_StopStartup, "startup-boundary-reached"):
            await runtime.start()

        self.assertLess(
            events.index("sentinel"),
            events.index("deleting-conversations-recovered"),
        )
        self.assertLess(
            events.index("deleting-conversations-recovered"),
            events.index("submission-projected"),
        )
        self.assertLess(events.index("submission-projected"), events.index("mcp-admit"))
        self.assertLess(events.index("mcp-reconciled"), events.index("submission-handoff"))
        self.assertLess(events.index("submission-handoff"), events.index("agent-recovery"))
        self.assertNotIn("submission-abort", events)

    async def test_startup_waits_for_delete_recovery_before_pending_projection(self) -> None:
        events: list[str] = []
        coordinator = AsyncMock()
        runtime = self._startup_runtime(events, coordinator)
        recovery_started = asyncio.Event()
        release_recovery = asyncio.Event()

        async def recover_deleting_conversations() -> None:
            recovery_started.set()
            await release_recovery.wait()

        runtime.recover_deleting_conversations = recover_deleting_conversations
        startup = asyncio.create_task(runtime.start())
        await recovery_started.wait()

        coordinator.project_pending.assert_not_awaited()
        release_recovery.set()
        with self.assertRaisesRegex(_StopStartup, "startup-boundary-reached"):
            await startup
        coordinator.project_pending.assert_awaited_once_with()

    async def test_failure_between_projection_and_handoff_aborts_claims(self) -> None:
        coordinator = AsyncMock()
        events: list[str] = []
        runtime = self._startup_runtime(events, coordinator)
        runtime._admit_mcp_rollout_instance = AsyncMock(
            side_effect=RuntimeError("mcp-startup-failed")
        )

        with self.assertRaisesRegex(RuntimeError, "mcp-startup-failed"):
            await runtime.start()

        coordinator.project_pending.assert_awaited_once_with()
        coordinator.recover_projected_handoffs.assert_not_awaited()
        coordinator.abort_pending.assert_awaited_once_with()

    async def test_handoff_failure_aborts_claims_and_never_recovers_agent_runs(self) -> None:
        events: list[str] = []
        coordinator = AsyncMock()
        coordinator.recover_projected_handoffs.side_effect = RuntimeError(
            "submission-handoff-failed"
        )
        runtime = self._startup_runtime(events, coordinator)

        with self.assertRaisesRegex(RuntimeError, "submission-handoff-failed"):
            await runtime.start()

        coordinator.abort_pending.assert_awaited_once_with()
        self.assertNotIn("agent-recovery", events)


class AgentRunLeaseRetryStartupTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _run(*, status: AgentRunStatus, lease_expires_at: datetime | None) -> AgentRun:
        return AgentRun(
            run_id="agent-run:task-1",
            task_id="task-1",
            conversation_id="conversation-1",
            status=status,
            binding=AgentModelBinding(model_edition="test-model"),
            lease_expires_at=lease_expires_at,
        )

    def _runtime(
        self,
        *,
        initial_run: AgentRun,
        recover: object,
    ) -> tuple[ApiRuntime, SimpleNamespace]:
        state = SimpleNamespace(current=initial_run)

        class Repository:
            async def list_recoverable_runs(_self):
                return (initial_run,)

            async def get_run(_self, run_id: str):
                self.assertEqual(run_id, initial_run.run_id)
                return state.current

        task = Task(
            task_id="task-1",
            conversation_id="conversation-1",
            root_message_id="message-1",
            status=TaskStatus.RUNNING,
        )
        conversation = Conversation(
            conversation_id="conversation-1",
            username="alice",
            current_task_id="task-1",
        )
        message = Message(
            message_id="message-1",
            conversation_id="conversation-1",
            role=MessageRole.USER,
            content="hello",
            task_id="task-1",
        )

        class Storage:
            async def get_task(_self, task_id: str):
                self.assertEqual(task_id, task.task_id)
                return task

            async def get_conversation(_self, conversation_id: str):
                self.assertEqual(conversation_id, conversation.conversation_id)
                return conversation

            async def get_message(_self, message_id: str):
                self.assertEqual(message_id, message.message_id)
                return message

        runtime = object.__new__(ApiRuntime)
        runtime.agent_run_repository = Repository()
        runtime.storage = Storage()

        async def recover_with_result(run_id: str, **kwargs):
            result = await recover(run_id, **kwargs)
            return result or SimpleNamespace(run=state.current)

        runtime._agent_run_recovery = SimpleNamespace(
            recover_crashed_run=recover_with_result
        )
        runtime._agent_invocation_contexts = SimpleNamespace(merge=lambda *_args, **_kwargs: None)
        runtime._task_accepted_llm_metadata = AsyncMock(return_value={})
        runtime._mcp_task_assignment_metadata = lambda _task: {}
        runtime._skill_runtime_state = None
        runtime._mcp_runtime_state = None
        runtime._task_skill_bundle_revisions = {}
        runtime._task_mcp_bundle_revisions = {}
        runtime.available_user_mcp_server_profiles = AsyncMock(return_value=())
        runtime._agent_cancellation_token = lambda _task_id: None
        runtime._agent_run_lease_retry_tasks = {}
        runtime._agent_run_lease_retry_errors = {}
        runtime._agent_run_lease_retry_sleep = asyncio.sleep
        runtime._agent_run_recovery_fatal_exit = Mock()
        runtime._clear_conversation_current_task = AsyncMock()
        return runtime, state

    async def test_heartbeat_extension_is_observed_before_expiry_recovery(self) -> None:
        clock = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
        initial = self._run(
            status=AgentRunStatus.RUNNING,
            lease_expires_at=clock + timedelta(seconds=10),
        )
        recovery_calls = 0

        async def recover(_run_id: str, **_kwargs) -> None:
            nonlocal recovery_calls
            recovery_calls += 1
            if recovery_calls == 1:
                raise AgentStorageConflict("agent_task_lease_held")

        runtime, state = self._runtime(initial_run=initial, recover=recover)
        now = clock
        delays: list[float] = []

        async def sleep(delay: float) -> None:
            nonlocal now
            delays.append(delay)
            now += timedelta(seconds=delay)
            if len(delays) == 1:
                state.current = replace(
                    state.current,
                    lease_expires_at=clock + timedelta(seconds=20),
                )

        runtime._utcnow_naive = lambda: now.replace(tzinfo=None)
        runtime._agent_run_lease_retry_sleep = sleep

        await runtime._recover_agent_runs()
        retry = runtime._agent_run_lease_retry_tasks[initial.run_id]
        await retry

        self.assertEqual(delays, [10.0, 10.0])
        self.assertEqual(recovery_calls, 2)

    async def test_retry_observer_is_unique_per_run_id(self) -> None:
        runtime = object.__new__(ApiRuntime)
        runtime._agent_run_lease_retry_tasks = {}
        runtime._agent_run_lease_retry_errors = {}
        release = asyncio.Event()

        async def observe(_run_id: str) -> None:
            await release.wait()

        runtime._observe_agent_run_lease = observe

        runtime._schedule_agent_run_lease_retry("agent-run:task-1")
        first = runtime._agent_run_lease_retry_tasks["agent-run:task-1"]
        runtime._schedule_agent_run_lease_retry("agent-run:task-1")

        self.assertIs(
            runtime._agent_run_lease_retry_tasks["agent-run:task-1"], first
        )
        release.set()
        await first

    async def test_non_lease_conflict_still_fails_start_synchronously(self) -> None:
        events: list[str] = []
        runtime = SubmissionAdmissionRuntimeStartupTest._startup_runtime(
            events, None
        )
        run = SimpleNamespace(run_id="agent-run:hard")
        runtime.agent_run_repository = SimpleNamespace(
            list_recoverable_runs=AsyncMock(return_value=(run,))
        )

        async def recover(_run) -> None:
            raise AgentStorageConflict("agent_run_cas_mismatch")

        runtime._recover_agent_run = recover
        runtime._recover_agent_runs = ApiRuntime._recover_agent_runs.__get__(
            runtime, ApiRuntime
        )
        runtime._agent_run_lease_retry_tasks = {}
        runtime._agent_run_lease_retry_errors = {}
        runtime._agent_run_recovery_fatal_exit = Mock()

        with self.assertRaisesRegex(AgentStorageConflict, "cas_mismatch"):
            await runtime.start()

        self.assertEqual(runtime._agent_run_lease_retry_tasks, {})
        runtime._agent_run_recovery_fatal_exit.assert_not_called()


class PreparedAgentRunAndLeaseRetryRuntimeTest(unittest.IsolatedAsyncioTestCase):
    _run = staticmethod(AgentRunLeaseRetryStartupTest._run)
    _runtime = AgentRunLeaseRetryStartupTest._runtime

    async def test_prepared_recovery_rejects_deleted_task_bound_upload_before_agent_recovery(self) -> None:
        task = Task(
            task_id="task-prepared-deleted-upload",
            conversation_id="conversation-prepared-deleted-upload",
            root_message_id="message-prepared-deleted-upload",
            status=TaskStatus.RUNNING,
        )
        conversation = Conversation(task.conversation_id, "alice", current_task_id=task.task_id)
        root_message = Message(
            message_id=task.root_message_id,
            conversation_id=task.conversation_id,
            role=MessageRole.USER,
            content="root text",
            task_id=task.task_id,
        )
        run = AgentRun(
            run_id=f"agent-run:{task.task_id}",
            task_id=task.task_id,
            conversation_id=task.conversation_id,
            status=AgentRunStatus.RUNNING,
            binding=AgentModelBinding(
                model_edition="test-model",
                reasoning_effort="medium",
                thinking_enabled=False,
            ),
        )
        upload_ref = {
            "upload_id": "upl-deleted",
            "conversation_id": task.conversation_id,
            "sha256": "sha-deleted",
            "size_bytes": 12,
            "selected_sheet": None,
        }
        attachment = TaskInputAttachment(
            attachment_id="attachment-deleted",
            task_id=task.task_id,
            conversation_id=task.conversation_id,
            source_kind="conversation_file",
            source_upload_id="upl-deleted",
            filename="deleted.csv",
            content_type="text/csv",
            file_type="csv",
            size_bytes=12,
            sha256="sha-deleted",
            prompt_artifact={"upload_id": "upl-deleted", "status": "ready"},
            skill_artifact={"upload_id": "upl-deleted", "content": "frozen"},
        )
        deleted_resource = ConversationFileResource(
            file_id="upl-deleted",
            conversation_id=task.conversation_id,
            username="alice",
            original_filename="deleted.csv",
            content_type="text/csv",
            file_type="csv",
            size_bytes=12,
            sha256="sha-deleted",
            storage_key="conversation-prepared-deleted-upload/upl-deleted/original",
            status="deleted",
        )
        prepared = PreparedAgentRecoveryContext(
            username="alice",
            current_user_input="prepared execution text",
            initial_required_tool_name=None,
            model_options={
                "model_edition": "test-model",
                "reasoning_effort": "medium",
                "thinking_enabled": False,
            },
            bundle_revisions={
                "skill_bundle_revision": None,
                "mcp_bundle_revision": None,
            },
            execution_metadata={},
            memory_context=None,
            mcp_binding=None,
            mcp_assignment=None,
            available_mcp_servers=(),
            upload_refs=(upload_ref,),
            selected_upload_ids=("upl-deleted",),
        )

        class Storage:
            async def get_task(_self, _task_id: str):
                return task

            async def get_conversation(_self, _conversation_id: str):
                return conversation

            async def get_message(_self, _message_id: str):
                return root_message

            async def list_task_input_attachments_for_task(_self, _task_id: str):
                return [attachment]

            async def get_conversation_file_resource(
                _self, _conversation_id: str, _username: str, _upload_id: str
            ):
                return deleted_resource

        recovery = AsyncMock(return_value=SimpleNamespace(run=run))
        runtime = object.__new__(ApiRuntime)
        runtime.storage = Storage()
        runtime._prepared_agent_recovery_loader = SimpleNamespace(
            load=AsyncMock(return_value=prepared)
        )
        runtime._agent_invocation_contexts = SimpleNamespace(merge=Mock())
        runtime._agent_run_recovery = SimpleNamespace(recover_crashed_run=recovery)
        runtime._skill_runtime_state = None
        runtime._mcp_runtime_state = None
        runtime._task_skill_bundle_revisions = {}
        runtime._task_mcp_bundle_revisions = {}
        runtime._agent_cancellation_token = lambda _task_id: None

        with self.assertRaisesRegex(UploadValidationError, "upl-deleted"):
            await runtime._recover_agent_run(run)

        recovery.assert_not_awaited()
        runtime._agent_invocation_contexts.merge.assert_not_called()

    async def test_prepared_context_bypasses_current_inputs_and_restores_exact_facts(
        self,
    ) -> None:
        task = Task(
            task_id="task-prepared",
            conversation_id="conversation-prepared",
            root_message_id="message-prepared",
            status=TaskStatus.RUNNING,
            mcp_execution_mode="user_scoped",
            mcp_shadow_enabled=False,
            mcp_rollout_config_version="config-r1",
            mcp_route_reason_code="assigned",
            mcp_rollout_mode="enforce",
        )
        conversation = Conversation(
            conversation_id=task.conversation_id,
            username="alice",
            current_task_id=task.task_id,
        )
        root_message = Message(
            message_id=task.root_message_id,
            conversation_id=task.conversation_id,
            role=MessageRole.USER,
            content="root text",
            task_id=task.task_id,
        )
        run = AgentRun(
            run_id=f"agent-run:{task.task_id}",
            task_id=task.task_id,
            conversation_id=task.conversation_id,
            status=AgentRunStatus.RUNNING,
            binding=AgentModelBinding(
                model_edition="test-model",
                reasoning_effort="medium",
                thinking_enabled=False,
            ),
        )
        profile = UserMCPServerProfile(
            server_id="server-1",
            display_name="Server One",
            routing_description="prepared profile",
            transport="streamable_http",
        )
        prepared = PreparedAgentRecoveryContext(
            username="alice",
            current_user_input="prepared execution text",
            initial_required_tool_name="skill_required",
            model_options={
                "model_edition": "test-model",
                "reasoning_effort": "medium",
                "thinking_enabled": False,
            },
            bundle_revisions={
                "skill_bundle_revision": "skill-r7",
                "mcp_bundle_revision": "mcp-r9",
            },
            execution_metadata={
                "mcp_execution_mode": "user_scoped",
                "mcp_shadow_enabled": False,
                "mcp_rollout_config_version": "config-r1",
                "mcp_route_reason_code": "assigned",
                "mcp_rollout_mode": "enforce",
                "requested_capability_alias": None,
            },
            memory_context={"current_user_message": "frozen memory"},
            mcp_binding=None,
            mcp_assignment={
                "execution_mode": "user_scoped",
                "shadow_enabled": False,
                "rollout_config_version": "config-r1",
                "route_reason_code": "assigned",
                "rollout_mode": "enforce",
            },
            available_mcp_servers=(profile,),
        )

        class Storage:
            async def get_task(_self, task_id: str):
                self.assertEqual(task_id, task.task_id)
                return task

            async def get_conversation(_self, conversation_id: str):
                self.assertEqual(conversation_id, conversation.conversation_id)
                return conversation

            async def get_message(_self, message_id: str):
                self.assertEqual(message_id, root_message.message_id)
                return root_message

            async def list_task_input_attachments_for_task(
                _self, task_id: str
            ):
                self.assertEqual(task_id, task.task_id)
                return []

        class RevisionState:
            def __init__(self) -> None:
                self.retained: list[str] = []
                self.released: list[str] = []

            @property
            def active_revision(self):
                raise AssertionError("prepared recovery must not read active revision")

            def retain_revision(self, revision: str) -> None:
                self.retained.append(revision)

            def release_revision(self, revision: str) -> None:
                self.released.append(revision)

            def bundle_for_revision(self, revision: str) -> object:
                return {"revision": revision}

        loader = SimpleNamespace(load=AsyncMock(return_value=prepared))
        contexts = SimpleNamespace(merge=Mock())
        recovery = AsyncMock(return_value=SimpleNamespace(run=run))
        skill_state = RevisionState()
        mcp_state = RevisionState()
        runtime = object.__new__(ApiRuntime)
        runtime.storage = Storage()
        runtime._prepared_agent_recovery_loader = loader
        runtime._task_accepted_llm_metadata = AsyncMock(
            side_effect=AssertionError("prepared recovery must not read accepted event")
        )
        runtime.available_user_mcp_server_profiles = AsyncMock(
            side_effect=AssertionError("prepared recovery must not read current profiles")
        )
        runtime._agent_invocation_contexts = contexts
        runtime._agent_run_recovery = SimpleNamespace(
            recover_crashed_run=recovery
        )
        runtime._skill_runtime_state = skill_state
        runtime._mcp_runtime_state = mcp_state
        runtime._task_skill_bundle_revisions = {}
        runtime._task_mcp_bundle_revisions = {}
        runtime._agent_cancellation_token = lambda _task_id: "cancel-token"

        await runtime._recover_agent_run(run)

        loader.load.assert_awaited_once_with(
            username="alice",
            conversation_id=task.conversation_id,
            task_id=task.task_id,
            message_id=task.root_message_id,
            root_message_content="root text",
        )
        contexts.merge.assert_called_once()
        merge_kwargs = contexts.merge.call_args.kwargs
        expected_scope = ApiRuntime._agent_owner_scope("alice")
        self.assertEqual(merge_kwargs["current_user_input"], "prepared execution text")
        self.assertEqual(merge_kwargs["metadata"]["agent_owner_scope"], expected_scope)
        self.assertNotEqual(merge_kwargs["metadata"]["agent_owner_scope"], "alice")
        self.assertEqual(
            merge_kwargs["metadata"]["conversation_memory"],
            {"current_user_message": "frozen memory"},
        )
        self.assertEqual(
            merge_kwargs["metadata"]["available_mcp_server_ids"], ["server-1"]
        )
        recovery.assert_awaited_once()
        recovery_kwargs = recovery.await_args.kwargs
        self.assertEqual(
            recovery_kwargs["initial_required_tool_name"], "skill_required"
        )
        self.assertEqual(
            recovery_kwargs["trusted_facts"],
            ('{"conversation_memory":{"current_user_message":"frozen memory"}}',),
        )
        visibility = recovery_kwargs["visibility_context"]
        self.assertEqual(visibility.authenticated_owner_scope, expected_scope)
        self.assertEqual(visibility.pinned_skill_bundle_revision, "skill-r7")
        self.assertEqual(visibility.safe_mcp_server_profiles, (profile,))
        self.assertEqual(skill_state.retained, ["skill-r7"])
        self.assertEqual(mcp_state.retained, ["mcp-r9"])
        self.assertEqual(
            runtime._task_skill_bundle_revisions, {task.task_id: "skill-r7"}
        )
        self.assertEqual(
            runtime._task_mcp_bundle_revisions, {task.task_id: "mcp-r9"}
        )

    async def test_prepared_context_binding_or_assignment_drift_fails_before_recovery(
        self,
    ) -> None:
        run = AgentRun(
            run_id="agent-run:task-drift",
            task_id="task-drift",
            conversation_id="conversation-drift",
            status=AgentRunStatus.RUNNING,
            binding=AgentModelBinding(model_edition="durable-model"),
        )
        conversation = Conversation(
            conversation_id=run.conversation_id,
            username="alice",
        )
        task = Task(
            task_id=run.task_id,
            conversation_id=run.conversation_id,
            root_message_id="message-drift",
            status=TaskStatus.RUNNING,
        )
        base = PreparedAgentRecoveryContext(
            username="alice",
            current_user_input="prepared text",
            initial_required_tool_name=None,
            model_options={
                "model_edition": "other-model",
                "reasoning_effort": "minimal",
                "thinking_enabled": False,
            },
            bundle_revisions={
                "skill_bundle_revision": None,
                "mcp_bundle_revision": None,
            },
            execution_metadata={},
            memory_context=None,
            mcp_binding=None,
            mcp_assignment=None,
            available_mcp_servers=(),
        )
        runtime = object.__new__(ApiRuntime)
        runtime.storage = SimpleNamespace(
            get_task=AsyncMock(return_value=task),
            get_conversation=AsyncMock(return_value=conversation),
            get_message=AsyncMock(return_value=Message(
                message_id=task.root_message_id,
                conversation_id=task.conversation_id,
                role=MessageRole.USER,
                content="root",
                task_id=task.task_id,
            )),
        )
        runtime._prepared_agent_recovery_loader = SimpleNamespace(
            load=AsyncMock(return_value=base)
        )
        runtime._agent_invocation_contexts = SimpleNamespace(merge=Mock())
        runtime._agent_run_recovery = SimpleNamespace(
            recover_crashed_run=AsyncMock()
        )

        with self.assertRaisesRegex(RuntimeError, "model_binding_mismatch"):
            await runtime._recover_agent_run(run)
        runtime._agent_run_recovery.recover_crashed_run.assert_not_awaited()

    async def test_prepared_terminal_recovery_releases_restored_bundle_revisions(
        self,
    ) -> None:
        task = Task(
            task_id="task-terminal",
            conversation_id="conversation-terminal",
            root_message_id="message-terminal",
            status=TaskStatus.COMPLETED,
        )
        conversation = Conversation(
            conversation_id=task.conversation_id,
            username="alice",
        )
        root = Message(
            message_id=task.root_message_id,
            conversation_id=task.conversation_id,
            role=MessageRole.USER,
            content="root",
            task_id=task.task_id,
        )
        run = AgentRun(
            run_id=f"agent-run:{task.task_id}",
            task_id=task.task_id,
            conversation_id=task.conversation_id,
            status=AgentRunStatus.RUNNING,
            binding=AgentModelBinding(
                model_edition="test-model",
                reasoning_effort="medium",
            ),
        )
        terminal_run = replace(run, status=AgentRunStatus.COMPLETED)
        prepared = PreparedAgentRecoveryContext(
            username="alice",
            current_user_input="prepared",
            initial_required_tool_name=None,
            model_options={
                "model_edition": "test-model",
                "reasoning_effort": "medium",
                "thinking_enabled": False,
            },
            bundle_revisions={
                "skill_bundle_revision": "skill-r1",
                "mcp_bundle_revision": "mcp-r1",
            },
            execution_metadata={},
            memory_context=None,
            mcp_binding=None,
            mcp_assignment=None,
            available_mcp_servers=(),
        )

        class Storage:
            async def get_task(_self, _task_id: str):
                return task

            async def get_conversation(_self, _conversation_id: str):
                return conversation

            async def get_message(_self, _message_id: str):
                return root

            async def list_task_input_attachments_for_task(
                _self, _task_id: str
            ):
                return []

        class RevisionState:
            def __init__(self) -> None:
                self.retained: list[str] = []
                self.released: list[str] = []

            def bundle_for_revision(self, revision: str) -> object:
                return revision

            def retain_revision(self, revision: str) -> None:
                self.retained.append(revision)

            def release_revision(self, revision: str) -> None:
                self.released.append(revision)

        runtime = object.__new__(ApiRuntime)
        runtime.storage = Storage()
        runtime._prepared_agent_recovery_loader = SimpleNamespace(
            load=AsyncMock(return_value=prepared)
        )
        runtime._agent_invocation_contexts = SimpleNamespace(merge=Mock())
        runtime._agent_run_recovery = SimpleNamespace(
            recover_crashed_run=AsyncMock(
                return_value=SimpleNamespace(run=terminal_run)
            )
        )
        runtime._skill_runtime_state = RevisionState()
        runtime._mcp_runtime_state = RevisionState()
        runtime._task_skill_bundle_revisions = {}
        runtime._task_mcp_bundle_revisions = {}
        runtime._agent_cancellation_token = lambda _task_id: None
        runtime._release_bundle_revision_with_sidecar_if_enforced = Mock()
        runtime._record_bundle_revision_shadow = Mock()
        runtime._clear_conversation_current_task = AsyncMock()

        await runtime._recover_agent_run(run)

        self.assertEqual(runtime._task_skill_bundle_revisions, {})
        self.assertEqual(runtime._task_mcp_bundle_revisions, {})
        self.assertEqual(runtime._skill_runtime_state.retained, ["skill-r1"])
        self.assertEqual(runtime._skill_runtime_state.released, ["skill-r1"])
        self.assertEqual(runtime._mcp_runtime_state.retained, ["mcp-r1"])
        self.assertEqual(runtime._mcp_runtime_state.released, ["mcp-r1"])
        runtime._clear_conversation_current_task.assert_awaited_once_with(
            task.conversation_id,
            task.task_id,
        )

    async def test_observer_terminal_fast_path_releases_restored_revisions(self) -> None:
        run = AgentRun(
            run_id="agent-run:task-observed-terminal",
            task_id="task-observed-terminal",
            conversation_id="conversation-observed-terminal",
            status=AgentRunStatus.COMPLETED,
            binding=AgentModelBinding(model_edition="test-model"),
        )
        task = Task(
            task_id=run.task_id,
            conversation_id=run.conversation_id,
            root_message_id="message-observed-terminal",
            status=TaskStatus.COMPLETED,
        )

        class RevisionState:
            def __init__(self) -> None:
                self.released: list[str] = []

            def release_revision(self, revision: str) -> None:
                self.released.append(revision)

        runtime = object.__new__(ApiRuntime)
        runtime.agent_run_repository = SimpleNamespace(
            get_run=AsyncMock(return_value=run)
        )
        runtime.storage = SimpleNamespace(get_task=AsyncMock(return_value=task))
        runtime._skill_runtime_state = RevisionState()
        runtime._mcp_runtime_state = RevisionState()
        runtime._task_skill_bundle_revisions = {task.task_id: "skill-old"}
        runtime._task_mcp_bundle_revisions = {task.task_id: "mcp-old"}
        runtime._release_bundle_revision_with_sidecar_if_enforced = Mock()
        runtime._record_bundle_revision_shadow = Mock()
        runtime._clear_conversation_current_task = AsyncMock()

        await runtime._observe_agent_run_lease(run.run_id)

        self.assertEqual(runtime._task_skill_bundle_revisions, {})
        self.assertEqual(runtime._task_mcp_bundle_revisions, {})
        self.assertEqual(runtime._skill_runtime_state.released, ["skill-old"])
        self.assertEqual(runtime._mcp_runtime_state.released, ["mcp-old"])
        runtime._clear_conversation_current_task.assert_awaited_once_with(
            task.conversation_id,
            task.task_id,
        )

    def test_second_bundle_retain_failure_rolls_back_first_retain(self) -> None:
        class RevisionState:
            def __init__(self, *, fail_retain: bool = False) -> None:
                self.fail_retain = fail_retain
                self.retained: list[str] = []
                self.released: list[str] = []

            def bundle_for_revision(self, revision: str) -> object:
                return revision

            def retain_revision(self, revision: str) -> None:
                if self.fail_retain:
                    raise RuntimeError("retain_failed")
                self.retained.append(revision)

            def release_revision(self, revision: str) -> None:
                self.released.append(revision)

        runtime = object.__new__(ApiRuntime)
        runtime._skill_runtime_state = RevisionState()
        runtime._mcp_runtime_state = RevisionState(fail_retain=True)
        runtime._task_skill_bundle_revisions = {}
        runtime._task_mcp_bundle_revisions = {}

        with self.assertRaisesRegex(RuntimeError, "retain_failed"):
            runtime._restore_prepared_bundle_revisions(
                task_id="task-retain-failure",
                skill_revision="skill-r1",
                mcp_revision="mcp-r1",
            )

        self.assertEqual(runtime._task_skill_bundle_revisions, {})
        self.assertEqual(runtime._task_mcp_bundle_revisions, {})
        self.assertEqual(runtime._skill_runtime_state.retained, ["skill-r1"])
        self.assertEqual(runtime._skill_runtime_state.released, ["skill-r1"])

    async def test_zero_item_run_restores_initialization_from_prepared_authority(
        self,
    ) -> None:
        task = Task(
            task_id="task-zero-item",
            conversation_id="conversation-zero-item",
            root_message_id="message-zero-item",
            status=TaskStatus.RUNNING,
            routing_mode="hint",
            requested_capability_id="skill.one",
        )
        conversation = Conversation(
            conversation_id=task.conversation_id,
            username="alice",
        )
        root = Message(
            message_id=task.root_message_id,
            conversation_id=task.conversation_id,
            role=MessageRole.USER,
            content="current root text",
            task_id=task.task_id,
        )
        run = AgentRun(
            run_id=f"agent-run:{task.task_id}",
            task_id=task.task_id,
            conversation_id=task.conversation_id,
            status=AgentRunStatus.RUNNING,
            binding=AgentModelBinding("prepared-model"),
        )
        activation_json = '{"binding_mode":"hint"}\n'
        prepared = PreparedAgentRecoveryContext(
            username="alice",
            current_user_input="prepared user text",
            initial_required_tool_name=None,
            model_options={
                "model_edition": "prepared-model",
                "reasoning_effort": "minimal",
                "thinking_enabled": False,
            },
            bundle_revisions={
                "skill_bundle_revision": None,
                "mcp_bundle_revision": None,
            },
            execution_metadata={"prepared_fact": "kept"},
            memory_context={
                "current_user_message": "prepared current text",
                "resolved_user_message": "prepared resolved text",
            },
            mcp_binding=None,
            mcp_assignment=None,
            available_mcp_servers=(),
            skill_activation_payload_json=activation_json,
            skill_activation_payload_sha256="a" * 64,
        )

        class Storage:
            async def get_task(_self, _task_id: str):
                return task

            async def get_conversation(_self, _conversation_id: str):
                return conversation

            async def get_message(_self, _message_id: str):
                return root

            async def list_task_input_attachments_for_task(
                _self, _task_id: str
            ):
                return []

        initialize = AsyncMock(return_value=SimpleNamespace(run=run))
        recover = AsyncMock(return_value=SimpleNamespace(run=run))
        runtime = object.__new__(ApiRuntime)
        runtime.storage = Storage()
        runtime.agent_run_repository = SimpleNamespace(
            list_items=AsyncMock(return_value=())
        )
        runtime._prepared_agent_recovery_loader = SimpleNamespace(
            load=AsyncMock(return_value=prepared)
        )
        runtime.agent_loop_orchestrator = SimpleNamespace(
            initialize_run=initialize
        )
        runtime._agent_invocation_contexts = SimpleNamespace(merge=Mock())
        runtime._agent_run_recovery = SimpleNamespace(
            recover_crashed_run=recover
        )
        runtime._skill_runtime_state = None
        runtime._mcp_runtime_state = None
        runtime._task_skill_bundle_revisions = {}
        runtime._task_mcp_bundle_revisions = {}
        runtime._agent_cancellation_token = lambda _task_id: None

        await runtime._recover_agent_run(run)

        initialize.assert_awaited_once()
        request = initialize.await_args.args[0]
        self.assertEqual(request.user_message, "prepared user text")
        self.assertEqual(request.current_user_message, "prepared current text")
        self.assertEqual(request.resolved_user_message, "prepared resolved text")
        self.assertEqual(request.memory_context, prepared.memory_context)
        self.assertEqual(request.requested_capability_id, "skill.one")
        self.assertEqual(
            request.skill_activation_payload_json, activation_json
        )
        self.assertEqual(
            request.skill_activation_payload_sha256, "a" * 64
        )
        recover.assert_awaited_once()

    async def test_zero_item_run_without_prepared_authority_fails_closed(
        self,
    ) -> None:
        task = Task(
            task_id="task-zero-item-missing",
            conversation_id="conversation-zero-item-missing",
            root_message_id="message-zero-item-missing",
            status=TaskStatus.RUNNING,
        )
        conversation = Conversation(
            conversation_id=task.conversation_id,
            username="alice",
        )
        run = AgentRun(
            run_id=f"agent-run:{task.task_id}",
            task_id=task.task_id,
            conversation_id=task.conversation_id,
            status=AgentRunStatus.RUNNING,
            binding=AgentModelBinding("prepared-model"),
        )
        initialize = AsyncMock()
        runtime = object.__new__(ApiRuntime)
        runtime.storage = SimpleNamespace(
            get_task=AsyncMock(return_value=task),
            get_conversation=AsyncMock(return_value=conversation),
            get_message=AsyncMock(return_value=None),
        )
        runtime.agent_run_repository = SimpleNamespace(
            list_items=AsyncMock(return_value=())
        )
        runtime._prepared_agent_recovery_loader = SimpleNamespace(
            load=AsyncMock(return_value=None)
        )
        runtime.agent_loop_orchestrator = SimpleNamespace(
            initialize_run=initialize
        )

        with self.assertRaisesRegex(
            RuntimeError, "agent_startup_initialization_authority_missing"
        ):
            await runtime._recover_agent_run(run)

        initialize.assert_not_awaited()


class AgentRunLeaseRetryFailureBoundaryTest(unittest.IsolatedAsyncioTestCase):
    _run = staticmethod(AgentRunLeaseRetryStartupTest._run)
    _runtime = AgentRunLeaseRetryStartupTest._runtime

    async def test_background_non_lease_failure_does_not_mutate_takeover_owner(self) -> None:
        clock = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
        initial = self._run(
            status=AgentRunStatus.RUNNING,
            lease_expires_at=clock,
        )
        recovery_calls = 0

        async def recover(_run_id: str, **_kwargs) -> None:
            nonlocal recovery_calls
            recovery_calls += 1
            if recovery_calls == 1:
                raise AgentStorageConflict("agent_task_lease_held")
            state.current = replace(
                state.current,
                claim_owner="takeover-owner",
                claim_token="takeover-token",
                lease_expires_at=clock + timedelta(seconds=30),
            )
            raise RuntimeError("agent-background-recovery-failed")

        runtime, state = self._runtime(initial_run=initial, recover=recover)
        runtime._utcnow_naive = lambda: clock.replace(tzinfo=None)
        runtime._mark_task_failed = AsyncMock()
        runtime._clear_conversation_current_task = AsyncMock()
        runtime._release_task_skill_revision_if_terminal = AsyncMock()
        runtime._release_task_mcp_revision_if_terminal = AsyncMock()
        runtime._agent_run_recovery_fatal_exit = Mock()

        await runtime._recover_agent_runs()
        retry = runtime._agent_run_lease_retry_tasks[initial.run_id]
        with self.assertLogs("src.api.runtime", level="ERROR"):
            await retry
        await asyncio.sleep(0)

        runtime._mark_task_failed.assert_not_awaited()
        runtime._clear_conversation_current_task.assert_not_awaited()
        runtime._release_task_skill_revision_if_terminal.assert_not_awaited()
        runtime._release_task_mcp_revision_if_terminal.assert_not_awaited()
        self.assertEqual(state.current.status, AgentRunStatus.RUNNING)
        self.assertEqual(state.current.claim_owner, "takeover-owner")
        self.assertEqual(state.current.claim_token, "takeover-token")
        runtime._agent_run_recovery_fatal_exit.assert_called_once_with(70)
        self.assertEqual(
            runtime._agent_run_lease_retry_errors[initial.run_id], "RuntimeError"
        )

    async def test_start_cancels_held_observer_when_later_run_hard_fails(self) -> None:
        events: list[str] = []
        runtime = SubmissionAdmissionRuntimeStartupTest._startup_runtime(
            events, None
        )
        held = SimpleNamespace(run_id="agent-run:held")
        hard = SimpleNamespace(run_id="agent-run:hard")
        runtime.agent_run_repository = SimpleNamespace(
            list_recoverable_runs=AsyncMock(return_value=(held, hard))
        )
        attempts: list[str] = []

        async def recover(run) -> None:
            attempts.append(run.run_id)
            if run is held:
                raise AgentStorageConflict("agent_task_lease_held")
            raise RuntimeError("agent-hard-recovery-failure")

        runtime._recover_agent_run = recover
        runtime._agent_run_lease_retry_tasks = {}
        runtime._agent_run_lease_retry_errors = {}
        observer_ran = False

        async def observe(_run_id: str) -> None:
            nonlocal observer_ran
            observer_ran = True
            await asyncio.Event().wait()

        runtime._observe_agent_run_lease = observe
        created: list[asyncio.Task[None]] = []

        def schedule(run_id: str) -> None:
            ApiRuntime._schedule_agent_run_lease_retry(runtime, run_id)
            created.append(runtime._agent_run_lease_retry_tasks[run_id])

        runtime._schedule_agent_run_lease_retry = schedule
        runtime._recover_agent_runs = ApiRuntime._recover_agent_runs.__get__(
            runtime, ApiRuntime
        )

        with self.assertRaisesRegex(RuntimeError, "agent-hard-recovery-failure"):
            await runtime.start()

        self.assertEqual(attempts, [held.run_id, hard.run_id])
        self.assertEqual(len(created), 1)
        self.assertTrue(created[0].cancelled())
        self.assertFalse(observer_ran)
        self.assertEqual(runtime._agent_run_lease_retry_tasks, {})

    async def test_start_cancels_live_observer_on_post_recovery_failure(self) -> None:
        events: list[str] = []
        runtime = SubmissionAdmissionRuntimeStartupTest._startup_runtime(
            events, None
        )
        runtime._agent_run_lease_retry_tasks = {}
        runtime._agent_run_lease_retry_errors = {}
        observer_started = asyncio.Event()
        observer_cancelled = asyncio.Event()

        async def observe(_run_id: str) -> None:
            observer_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                observer_cancelled.set()

        runtime._observe_agent_run_lease = observe
        observer: asyncio.Task[None] | None = None

        async def recover_runs() -> None:
            nonlocal observer
            runtime._schedule_agent_run_lease_retry("agent-run:held")
            observer = runtime._agent_run_lease_retry_tasks["agent-run:held"]
            await observer_started.wait()

        runtime._recover_agent_runs = recover_runs
        runtime._mcp_cp7_safety_facade = object()
        runtime._mcp_cp7_open_boundary = None

        with self.assertRaisesRegex(RuntimeError, "mcp_cp7_open_boundary_missing"):
            await runtime.start()

        self.assertIsNotNone(observer)
        self.assertTrue(observer.cancelled())
        self.assertTrue(observer_cancelled.is_set())
        self.assertEqual(runtime._agent_run_lease_retry_tasks, {})

    async def test_terminal_and_waiting_runs_stop_without_second_recovery(self) -> None:
        for final_status in (
            AgentRunStatus.COMPLETED,
            AgentRunStatus.WAITING_FOR_INPUT,
            AgentRunStatus.WAITING_FOR_DEPENDENCY,
        ):
            with self.subTest(final_status=final_status):
                clock = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
                initial = self._run(
                    status=AgentRunStatus.RUNNING,
                    lease_expires_at=clock + timedelta(seconds=5),
                )
                recovery_calls = 0

                async def recover(_run_id: str, **_kwargs) -> None:
                    nonlocal recovery_calls
                    recovery_calls += 1
                    raise AgentStorageConflict("agent_task_lease_held")

                runtime, state = self._runtime(initial_run=initial, recover=recover)
                runtime._utcnow_naive = lambda: clock.replace(tzinfo=None)

                async def sleep(_delay: float) -> None:
                    state.current = replace(state.current, status=final_status)

                runtime._agent_run_lease_retry_sleep = sleep
                await runtime._recover_agent_runs()
                retry = runtime._agent_run_lease_retry_tasks[initial.run_id]
                await retry

                self.assertEqual(recovery_calls, 1)

    async def test_shutdown_aborts_submission_and_cancels_lease_observer(self) -> None:
        clock = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
        initial = self._run(
            status=AgentRunStatus.RUNNING,
            lease_expires_at=clock + timedelta(seconds=30),
        )

        async def recover(_run_id: str, **_kwargs) -> None:
            raise AgentStorageConflict("agent_task_lease_held")

        runtime, _state = self._runtime(initial_run=initial, recover=recover)
        runtime._utcnow_naive = lambda: clock.replace(tzinfo=None)
        sleep_started = asyncio.Event()
        sleep_cancelled = asyncio.Event()

        async def sleep(_delay: float) -> None:
            sleep_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                sleep_cancelled.set()

        runtime._agent_run_lease_retry_sleep = sleep
        await runtime._recover_agent_runs()
        await sleep_started.wait()
        coordinator = AsyncMock()
        runtime._submission_admission_coordinator = coordinator
        runtime._quiesce_cp7_for_shutdown = AsyncMock(
            side_effect=_StopStartup("shutdown-boundary-reached")
        )

        with self.assertRaisesRegex(_StopStartup, "shutdown-boundary-reached"):
            await runtime.shutdown()

        coordinator.abort_pending.assert_awaited_once_with()
        self.assertTrue(sleep_cancelled.is_set())
        self.assertEqual(runtime._agent_run_lease_retry_tasks, {})


if __name__ == "__main__":
    unittest.main()
