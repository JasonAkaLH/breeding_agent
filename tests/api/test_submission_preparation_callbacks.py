from __future__ import annotations

import asyncio
import hashlib
import json
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from src.api.runtime import ApiRuntime
from src.api.submission_admission import PreparedAgentRecoveryContext
from src.core.enums import EventVisibility, InterruptStatus, NodeStatus, RoutingMode, TaskStatus
from src.core.models import (
    EventRecord,
    MCPNoServerConvergenceResult,
    SubmissionRecoveryRecord,
    Task,
    TaskInputAttachment,
)
from src.orchestration.agent_loop.models import (
    AgentModelBinding,
    AgentRun,
    AgentRunStatus,
)


NOW = datetime(2026, 8, 27, 12, 0, 0)


def _record(*, continuation: dict[str, object] | None = None) -> SubmissionRecoveryRecord:
    return SimpleNamespace(
        username="alice",
        conversation_id="conversation-1",
        message_id="message-1",
        task_id="task-1",
        message_projection=json.dumps(
            {
                "content": "hello",
                "metadata": {
                    "__maf_private_submission_input_v1": {
                        "explicit_upload_ids": [],
                        "selector_metadata": {},
                    }
                },
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode(),
        continuation=json.dumps(
            continuation or {}, sort_keys=True, separators=(",", ":")
        ).encode(),
        prepared_execution_sha256="a" * 64,
        created_at=NOW,
    )


class _ExactEventStorage:
    def __init__(self) -> None:
        self.events: dict[str, EventRecord] = {}
        self.fail_append_once = False
        self.fail_event_type_once: str | None = None

    async def append_event_exact(self, event: EventRecord):
        if self.fail_append_once or self.fail_event_type_once == event.event_type:
            self.fail_append_once = False
            self.fail_event_type_once = None
            raise RuntimeError("event append fault")
        existing = self.events.get(event.event_id)
        if existing is not None:
            if existing != event:
                raise RuntimeError("event drift")
            return existing, True
        self.events[event.event_id] = event
        return event, False


class SubmissionPreparationCallbacksTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _terminal_prepared(kind: str) -> dict[str, object]:
        return {
            "planned_handoff_kind": kind,
            "requested_capability_id": None,
            "model_options": {
                "model_edition": "api-test",
                "reasoning_effort": "minimal",
                "thinking_enabled": False,
            },
            "bundle_revisions": {
                "skill_bundle_revision": "skillrev-000001-aaaaaaaaaaaa",
                "mcp_bundle_revision": None,
            },
            "execution_metadata": {},
            "skill_activation": None,
            "preparation_receipt": {"selector_decision_sha256": "b" * 64},
        }

    @staticmethod
    def _terminal_receipt(*, interrupt_kind: str | None = None):
        selector = (
            None
            if interrupt_kind is None
            else json.dumps(
                {"interrupt_kind": interrupt_kind},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        )
        return SimpleNamespace(selector_decision=selector)

    async def test_terminal_agent_handoff_creates_no_initial_items_or_sampling(self) -> None:
        task = Task(
            task_id="task-1",
            conversation_id="conversation-1",
            root_message_id="message-1",
            status=TaskStatus.ACCEPTED,
        )
        terminal_task = Task(
            task_id=task.task_id,
            conversation_id=task.conversation_id,
            root_message_id=task.root_message_id,
            status=TaskStatus.FAILED,
        )
        run = AgentRun(
            run_id="agent-run:task-1",
            task_id=task.task_id,
            conversation_id=task.conversation_id,
            status=AgentRunStatus.FAILED,
            binding=AgentModelBinding("api-test"),
            terminal_reason_code="agent_skill_bundle_revision_retired",
            terminal_at=NOW,
        )
        storage = SimpleNamespace(
            get_task=AsyncMock(side_effect=[task, terminal_task]),
        )
        initialize_terminal = AsyncMock(return_value=run)
        runtime = object.__new__(ApiRuntime)
        runtime.storage = storage
        runtime._agent_owner_scope = lambda username: f"owner:{username}"
        runtime.agent_loop_orchestrator = SimpleNamespace(
            initialize_terminal_run=initialize_terminal,
            initialize_run=AsyncMock(
                side_effect=AssertionError("normal Agent initialization must not run")
            ),
        )
        runtime._best_effort_clear_skill_recovery_pointer = AsyncMock()
        runtime._submission_selector_facts = {}
        runtime._submission_file_selection_computations = {}

        handoff = await runtime.materialize_terminal_handoff(
            _record(),
            self._terminal_prepared("agent_run"),
            self._terminal_receipt(),
            "agent_skill_bundle_revision_retired",
        )

        self.assertEqual(handoff.kind, "agent_run")
        self.assertEqual(handoff.identity, "agent-run:task-1")
        initialize_terminal.assert_awaited_once()
        request = initialize_terminal.await_args.args[0]
        self.assertEqual(
            request.metadata["skill_bundle_revision"],
            "skillrev-000001-aaaaaaaaaaaa",
        )
        self.assertEqual(
            initialize_terminal.await_args.kwargs["status"],
            AgentRunStatus.FAILED,
        )
        runtime.agent_loop_orchestrator.initialize_run.assert_not_awaited()

    async def test_terminal_interrupt_handoff_persists_only_cancelled_history(self) -> None:
        state = SimpleNamespace(
            task=Task(
                task_id="task-1",
                conversation_id="conversation-1",
                root_message_id="message-1",
                status=TaskStatus.ACCEPTED,
            ),
            node=None,
            interrupt=None,
        )

        async def save_task(task, *, expected_from_status):
            self.assertEqual(expected_from_status, TaskStatus.ACCEPTED)
            state.task = task
            return task

        async def save_node(node):
            state.node = node
            return node

        async def save_interrupt(interrupt):
            state.interrupt = interrupt
            return interrupt

        runtime = object.__new__(ApiRuntime)
        runtime.storage = SimpleNamespace(
            get_task=AsyncMock(side_effect=lambda _task_id: state.task),
            compare_and_set_task=save_task,
            get_task_node=AsyncMock(side_effect=lambda _node_id: state.node),
            save_task_node=save_node,
            get_interrupt=AsyncMock(side_effect=lambda _interrupt_id: state.interrupt),
            save_interrupt=save_interrupt,
        )
        runtime._utcnow_naive = lambda: NOW
        runtime._best_effort_clear_skill_recovery_pointer = AsyncMock()
        runtime._submission_selector_facts = {}
        runtime._submission_file_selection_computations = {}

        handoff = await runtime.materialize_terminal_handoff(
            _record(),
            self._terminal_prepared("interrupt"),
            self._terminal_receipt(interrupt_kind="file_selection"),
            "agent_skill_bundle_revision_retired",
        )

        self.assertEqual(handoff.kind, "interrupt")
        self.assertEqual(state.task.status, TaskStatus.FAILED)
        self.assertEqual(state.node.status, NodeStatus.CANCELLED)
        self.assertEqual(state.interrupt.status, InterruptStatus.CANCELLED)
        self.assertEqual(state.interrupt.required_fields, {})

    async def test_terminal_no_server_handoff_reuses_exact_convergence(self) -> None:
        state = SimpleNamespace(
            task=Task(
                task_id="task-1",
                conversation_id="conversation-1",
                root_message_id="message-1",
                status=TaskStatus.ACCEPTED,
            )
        )

        async def converge(**_kwargs):
            state.task = Task(
                task_id=state.task.task_id,
                conversation_id=state.task.conversation_id,
                root_message_id=state.task.root_message_id,
                status=TaskStatus.FAILED,
            )
            return MCPNoServerConvergenceResult.CONVERGED

        runtime = object.__new__(ApiRuntime)
        runtime.storage = SimpleNamespace(
            get_task=AsyncMock(side_effect=lambda _task_id: state.task),
            converge_submission_no_server_handoff_exact=AsyncMock(
                side_effect=converge
            ),
        )
        runtime._best_effort_clear_skill_recovery_pointer = AsyncMock()
        runtime._submission_selector_facts = {}
        runtime._submission_file_selection_computations = {}

        handoff = await runtime.materialize_terminal_handoff(
            _record(),
            self._terminal_prepared("no_server_intent"),
            self._terminal_receipt(),
            "agent_skill_bundle_revision_retired",
        )

        self.assertEqual(handoff.kind, "no_server_intent")
        self.assertEqual(state.task.status, TaskStatus.FAILED)
        runtime.storage.converge_submission_no_server_handoff_exact.assert_awaited_once()
    async def test_prepared_agent_request_restores_persisted_artifacts_for_skill(
        self,
    ) -> None:
        prompt_artifact = {
            "upload_id": "upload-1",
            "filename": "pedigree.csv",
            "preview": {"columns": ["ped_id"]},
        }
        skill_artifact = {
            **prompt_artifact,
            "content": "ped_id\nA001\n",
        }
        attachment = TaskInputAttachment(
            attachment_id="task-1:input:upload-1",
            task_id="task-1",
            conversation_id="conversation-1",
            source_kind="message_upload",
            source_upload_id="upload-1",
            filename="pedigree.csv",
            content_type="text/csv",
            file_type="csv",
            size_bytes=12,
            sha256="b" * 64,
            prompt_artifact=prompt_artifact,
            skill_artifact=skill_artifact,
            created_at=NOW,
            updated_at=NOW,
        )
        runtime = object.__new__(ApiRuntime)
        runtime.storage = SimpleNamespace(
            list_task_input_attachments_for_task=AsyncMock(
                return_value=[attachment]
            )
        )
        runtime._agent_owner_scope = lambda username: f"owner:{username}"
        continuation = {
            "execution_metadata": {},
            "model_options": {},
            "bundle_revisions": {},
            "upload_refs": [
                {
                    "upload_id": "upload-1",
                    "conversation_id": "conversation-1",
                    "sha256": "b" * 64,
                    "size_bytes": 12,
                    "selected_sheet": None,
                }
            ],
            "available_mcp_servers": [],
            "requested_capability_id": "skill.example",
        }

        request = await runtime._submission_agent_request(
            _record(), continuation, user_message="run the skill"
        )

        self.assertEqual(request.metadata["uploaded_artifacts"], [prompt_artifact])
        self.assertEqual(request.metadata["skill_artifacts"], [skill_artifact])
        self.assertEqual(
            request.metadata["skill_artifacts"][0]["content"],
            "ped_id\nA001\n",
        )
        self.assertNotEqual(
            request.metadata["uploaded_artifacts"], continuation["upload_refs"]
        )

    async def test_startup_attachment_restore_rejects_unexpected_upload(self) -> None:
        selected = TaskInputAttachment(
            attachment_id="task-1:input:selected",
            task_id="task-1",
            conversation_id="conversation-1",
            source_kind="file_selector",
            source_upload_id="selected",
            size_bytes=3,
            sha256="a" * 64,
            prompt_artifact={"upload_id": "selected"},
            skill_artifact={"upload_id": "selected", "content": "ok"},
        )
        unexpected = TaskInputAttachment(
            attachment_id="task-1:input:unexpected",
            task_id="task-1",
            conversation_id="conversation-1",
            source_kind="message_upload",
            source_upload_id="unexpected",
            size_bytes=6,
            sha256="b" * 64,
            prompt_artifact={"upload_id": "unexpected"},
            skill_artifact={"upload_id": "unexpected", "content": "SECRET"},
        )
        runtime = object.__new__(ApiRuntime)
        runtime.storage = SimpleNamespace(
            list_task_input_attachments_for_task=AsyncMock(
                return_value=[selected, unexpected]
            )
        )

        with self.assertRaisesRegex(
            RuntimeError, "submission_attachment_selection_drift"
        ):
            await runtime._prepared_task_input_attachment_metadata(
                "task-1",
                upload_refs=(
                    {
                        "upload_id": "selected",
                        "conversation_id": "conversation-1",
                        "sha256": "a" * 64,
                        "size_bytes": 3,
                        "selected_sheet": None,
                    },
                ),
                expected_upload_ids=("selected",),
            )

    async def test_memory_event_replay_materializes_and_publishes_once(self) -> None:
        storage = _ExactEventStorage()
        storage.materialize_conversation_memory_summary_exact = AsyncMock()
        runtime = object.__new__(ApiRuntime)
        runtime.storage = storage
        runtime.event_broker = SimpleNamespace(publish=AsyncMock())
        event = {
            "event_id": "submission-memory-event:v1:task-1:" + "a" * 64,
            "conversation_id": "conversation-1",
            "task_id": "task-1",
            "node_id": None,
            "agent_id": None,
            "event_type": "conversation.memory_built",
            "payload": {"resolved": False},
            "visibility": str(EventVisibility.AUDIT_ONLY),
            "created_at": NOW.isoformat(),
        }
        component = json.dumps(
            {
                "schema": "maf.submission.memory_preparation.v1",
                "prompt_payload": {},
                "summary_write": None,
                "event_write": event,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()

        await runtime.materialize_memory_context(_record(), component)
        await runtime.materialize_memory_context(_record(), component)

        self.assertEqual(len(storage.events), 1)
        runtime.event_broker.publish.assert_awaited_once()
        storage.materialize_conversation_memory_summary_exact.assert_not_awaited()

    async def test_route_replay_accepts_progressed_task_and_records_metric_once(self) -> None:
        continuation = {
            "routing_mode": str(RoutingMode.AUTO),
            "requested_capability_id": None,
            "mcp_assignment": {
                "execution_mode": "legacy",
                "shadow_enabled": False,
                "rollout_config_version": "config-1",
                "route_reason_code": "routing_off",
                "rollout_mode": "off",
            },
            "mcp_binding": None,
            "model_options": {
                "model_edition": "model-1",
                "reasoning_effort": "medium",
                "thinking_enabled": False,
            },
        }
        accepted = Task(
            task_id="task-1",
            conversation_id="conversation-1",
            root_message_id="message-1",
            status=TaskStatus.RUNNING,
            routing_mode=RoutingMode.AUTO,
            summary="hello",
            created_at=NOW,
            updated_at=NOW,
            mcp_execution_mode="legacy",
            mcp_shadow_enabled=False,
            mcp_rollout_config_version="config-1",
            mcp_route_reason_code="routing_off",
            mcp_rollout_mode="off",
        )
        storage = _ExactEventStorage()
        storage.get_task = AsyncMock(return_value=accepted)
        storage.materialize_submission_pending_skill_transition_exact = AsyncMock()
        runtime = object.__new__(ApiRuntime)
        runtime.storage = storage
        runtime.event_broker = SimpleNamespace(publish=AsyncMock())
        runtime._mcp_audit_reference_signer = SimpleNamespace(
            safe_owner_reference=lambda *_args, **_kwargs: "safe-owner"
        )
        runtime._record_mcp_route_assignment_metric = AsyncMock()
        record = _record(continuation=continuation)

        await runtime.materialize_route_decision(record, b"{}")
        await runtime.materialize_route_decision(record, b"{}")

        self.assertEqual(len(storage.events), 2)
        self.assertFalse(
            any(
                event.event_type == "pending_skill_context.superseded"
                for event in storage.events.values()
            )
        )
        self.assertEqual(runtime.event_broker.publish.await_count, 2)
        runtime._record_mcp_route_assignment_metric.assert_awaited_once()
        storage.materialize_submission_pending_skill_transition_exact.assert_not_awaited()

    async def test_pending_supersede_receipt_is_published_only_after_first_transition(self) -> None:
        continuation = {
            "routing_mode": str(RoutingMode.FORCE_CAPABILITY),
            "requested_capability_id": "skill.example",
            "mcp_assignment": {
                "execution_mode": "legacy",
                "shadow_enabled": False,
                "rollout_config_version": "config-1",
                "route_reason_code": "routing_off",
                "rollout_mode": "off",
            },
            "mcp_binding": None,
            "model_options": {
                "model_edition": "model-1",
                "reasoning_effort": "medium",
                "thinking_enabled": False,
            },
        }
        task = Task(
            task_id="task-1",
            conversation_id="conversation-1",
            root_message_id="message-1",
            status=TaskStatus.ACCEPTED,
            routing_mode=RoutingMode.FORCE_CAPABILITY,
            requested_capability_id="skill.example",
            summary="hello",
            created_at=NOW,
            updated_at=NOW,
            mcp_execution_mode="legacy",
            mcp_shadow_enabled=False,
            mcp_rollout_config_version="config-1",
            mcp_route_reason_code="routing_off",
            mcp_rollout_mode="off",
        )
        storage = _ExactEventStorage()
        storage.get_task = AsyncMock(return_value=task)
        receipt = EventRecord(
            event_id="pending-transition-1",
            conversation_id="conversation-1",
            task_id="task-1",
            event_type="pending_skill_context.superseded",
            payload={
                "schema": "maf.pending_skill_context.transition_receipt.v1",
                "task_id": "task-1",
                "conversation_id": "conversation-1",
                "prepared_execution_sha256": "a" * 64,
                "context_ids_sha256": "b" * 64,
                "target_status": "superseded",
                "reason": "new_forced_capability",
                "occurred_at": NOW.isoformat(),
                "count": 1,
            },
            visibility=EventVisibility.AUDIT_ONLY,
            created_at=NOW,
        )
        storage.materialize_submission_pending_skill_transition_exact = AsyncMock(
            side_effect=[(receipt, False), (receipt, True)]
        )
        runtime = object.__new__(ApiRuntime)
        runtime.storage = storage
        runtime.event_broker = SimpleNamespace(publish=AsyncMock())
        runtime._mcp_audit_reference_signer = SimpleNamespace(
            safe_owner_reference=lambda *_args, **_kwargs: "safe-owner"
        )
        runtime._record_mcp_route_assignment_metric = AsyncMock()
        record = _record(continuation=continuation)

        await runtime.materialize_route_decision(record, b"{}")
        await runtime.materialize_route_decision(record, b"{}")

        self.assertEqual(len(storage.events), 2)
        self.assertEqual(runtime.event_broker.publish.await_count, 3)
        self.assertIn(receipt, [call.args[0] for call in runtime.event_broker.publish.await_args_list])
        self.assertEqual(
            storage.materialize_submission_pending_skill_transition_exact.await_count,
            2,
        )

    async def test_wakeup_replay_does_not_schedule_initialized_run_twice(self) -> None:
        runtime = object.__new__(ApiRuntime)
        initialized = SimpleNamespace(request=SimpleNamespace(task_id="task-1"))
        runtime._lock = asyncio.Lock()
        runtime._submission_initialized_agent_runs = {"task-1": initialized}
        runtime._submission_woken_agent_ids = set()
        runtime._submission_wakeup_flights = {}
        runtime._schedule_initialized_execution = AsyncMock()
        runtime.agent_run_repository = SimpleNamespace(get_run_for_task=AsyncMock())
        record = _record()

        await runtime.wakeup_agent(record, "agent-run:task-1")
        await runtime.wakeup_agent(record, "agent-run:task-1")

        runtime._schedule_initialized_execution.assert_awaited_once_with(initialized)
        runtime.agent_run_repository.get_run_for_task.assert_not_awaited()

    async def test_wakeup_schedule_failure_preserves_initialized_run_for_exact_retry(self) -> None:
        runtime = object.__new__(ApiRuntime)
        initialized = SimpleNamespace(request=SimpleNamespace(task_id="task-1"))
        runtime._lock = asyncio.Lock()
        runtime._submission_initialized_agent_runs = {"task-1": initialized}
        runtime._submission_woken_agent_ids = set()
        runtime._submission_wakeup_flights = {}
        runtime._schedule_initialized_execution = AsyncMock(
            side_effect=[RuntimeError("backpressure"), None]
        )
        runtime.agent_run_repository = SimpleNamespace(get_run_for_task=AsyncMock())
        record = _record()

        with self.assertRaisesRegex(RuntimeError, "backpressure"):
            await runtime.wakeup_agent(record, "agent-run:task-1")

        self.assertIs(
            runtime._submission_initialized_agent_runs["task-1"], initialized
        )
        self.assertNotIn("agent-run:task-1", runtime._submission_woken_agent_ids)

        await runtime.wakeup_agent(record, "agent-run:task-1")

        self.assertEqual(runtime._schedule_initialized_execution.await_count, 2)
        self.assertEqual(
            runtime._schedule_initialized_execution.await_args_list[0].args,
            (initialized,),
        )
        self.assertEqual(
            runtime._schedule_initialized_execution.await_args_list[1].args,
            (initialized,),
        )
        self.assertNotIn("task-1", runtime._submission_initialized_agent_runs)
        self.assertIn("agent-run:task-1", runtime._submission_woken_agent_ids)
        runtime.agent_run_repository.get_run_for_task.assert_not_awaited()

    async def test_selector_audit_replay_materializes_legacy_events_once(self) -> None:
        storage = _ExactEventStorage()
        runtime = object.__new__(ApiRuntime)
        runtime.storage = storage
        runtime.event_broker = SimpleNamespace(publish=AsyncMock())
        computation = SimpleNamespace(
            triggered=True,
            mode="shadow",
            invoked_payload={"mode": "shadow", "candidate_count": 1},
            invalid_output_payload={"reason_code": "invalid_json"},
            decision_payload={
                "mode": "shadow",
                "decision": "ambiguous",
                "reason_code": "invalid_json",
            },
        )
        facts = {
            "schema": "maf.submission.selector_materialization.v1",
            "explicit_upload_ids": [],
            "upload_refs": [],
            "pending_sheet_selections": [],
            "computation": {"mode": "shadow"},
            "winner": {
                "decision": "continue",
                "reason_code": "selector_shadow_observed",
                "resume_action": "resume",
                "upload_ids": [],
                "interrupt_kind": None,
            },
        }
        digest = hashlib.sha256(
            b"maf.submission.selector_winner.v1\0"
            + json.dumps(facts, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        winner = {
            **facts["winner"],
            "candidate_digest": digest,
        }
        runtime._submission_selector_facts = {"task-1": facts}
        runtime._submission_file_selection_computations = {
            "task-1": computation
        }
        component = json.dumps(
            winner, sort_keys=True, separators=(",", ":")
        ).encode()
        record = _record(continuation={"upload_refs": []})

        await runtime.materialize_selector_decision(record, component)
        await runtime.materialize_selector_decision(record, component)

        self.assertEqual(len(storage.events), 3)
        self.assertEqual(runtime.event_broker.publish.await_count, 3)

    async def test_selector_recovery_does_not_recompute_or_read_mutable_files(
        self,
    ) -> None:
        storage = _ExactEventStorage()
        storage.list_task_input_attachments_for_task = AsyncMock(return_value=[])
        storage.get_conversation_file_resource = AsyncMock(
            side_effect=AssertionError("mutable file resource was read")
        )
        runtime = object.__new__(ApiRuntime)
        runtime.storage = storage
        runtime.event_broker = SimpleNamespace(publish=AsyncMock())
        runtime._submission_selector_facts = {}
        runtime._submission_file_selection_computations = {}
        runtime._prepare_submission_selector = AsyncMock(
            side_effect=AssertionError("selector was recomputed")
        )
        winner = {
            "decision": "continue",
            "reason_code": "selector_not_triggered",
            "candidate_digest": "a" * 64,
            "resume_action": "resume",
            "upload_ids": [],
            "interrupt_kind": None,
        }

        await runtime.materialize_selector_decision(
            _record(continuation={"upload_refs": []}),
            json.dumps(winner, sort_keys=True, separators=(",", ":")).encode(),
        )

        runtime._prepare_submission_selector.assert_not_awaited()
        storage.get_conversation_file_resource.assert_not_awaited()

    async def test_selector_recovery_materializes_selected_ref_by_exact_identity(
        self,
    ) -> None:
        upload_id = "upload-1"
        content = b"ped_id\nA001\n"
        content_sha256 = hashlib.sha256(content).hexdigest()
        resource = SimpleNamespace(
            file_id=upload_id,
            status="active",
            conversation_id="conversation-1",
            sha256=content_sha256,
            size_bytes=len(content),
            storage_key="immutable/blob-1",
            selected_sheet=None,
        )
        task = Task(
            task_id="task-1",
            conversation_id="conversation-1",
            root_message_id="message-1",
            status=TaskStatus.ACCEPTED,
            routing_mode=RoutingMode.AUTO,
            created_at=NOW,
            updated_at=NOW,
        )
        attachment = TaskInputAttachment(
            attachment_id="task-1:input:upload-1",
            task_id="task-1",
            conversation_id="conversation-1",
            source_kind="file_selector",
            source_upload_id=upload_id,
            source_message_id="message-1",
            filename="pedigree.csv",
            content_type="text/csv",
            file_type="csv",
            size_bytes=len(content),
            sha256=content_sha256,
            prompt_artifact={"upload_id": upload_id},
            skill_artifact={"upload_id": upload_id, "content": "ped_id\nA001\n"},
            created_at=NOW,
            updated_at=NOW,
        )
        storage = _ExactEventStorage()
        storage.get_conversation_file_resource = AsyncMock(return_value=resource)
        storage.get_task = AsyncMock(return_value=task)
        storage.list_task_input_attachments_for_task = AsyncMock(return_value=[])
        storage.save_task_input_attachment = AsyncMock(return_value=attachment)
        runtime = object.__new__(ApiRuntime)
        runtime.storage = storage
        runtime.event_broker = SimpleNamespace(publish=AsyncMock())
        runtime.conversation_file_store = SimpleNamespace(
            read_bytes=Mock(return_value=content)
        )
        runtime._submission_selector_facts = {}
        runtime._submission_file_selection_computations = {}
        runtime._prepare_submission_selector = AsyncMock(
            side_effect=AssertionError("selector was recomputed")
        )
        upload_record = SimpleNamespace()
        runtime._upload_record_from_resource = Mock(return_value=upload_record)
        runtime._attachment_from_upload_record = Mock(return_value=attachment)
        continuation = {
            "upload_refs": [
                {
                    "upload_id": upload_id,
                    "conversation_id": "conversation-1",
                    "sha256": content_sha256,
                    "size_bytes": len(content),
                    "selected_sheet": None,
                }
            ]
        }
        winner = {
            "decision": "select_one",
            "reason_code": "selected",
            "candidate_digest": "a" * 64,
            "resume_action": "resume",
            "upload_ids": [upload_id],
            "interrupt_kind": None,
        }

        await runtime.materialize_selector_decision(
            _record(continuation=continuation),
            json.dumps(winner, sort_keys=True, separators=(",", ":")).encode(),
        )

        runtime._prepare_submission_selector.assert_not_awaited()
        storage.get_conversation_file_resource.assert_awaited_once_with(
            "conversation-1", "alice", upload_id
        )
        runtime.conversation_file_store.read_bytes.assert_called_once_with(
            "immutable/blob-1"
        )
        storage.save_task_input_attachment.assert_awaited_once_with(attachment)

    async def test_successful_agent_handoff_clears_selector_process_caches(
        self,
    ) -> None:
        storage = _ExactEventStorage()
        storage.list_task_input_attachments_for_task = AsyncMock(return_value=[])
        runtime = object.__new__(ApiRuntime)
        runtime.storage = storage
        runtime._agent_owner_scope = lambda username: f"owner:{username}"
        runtime._restore_prepared_bundle_revisions = Mock()
        runtime.agent_loop_orchestrator = SimpleNamespace(
            initialize_run=AsyncMock(return_value=SimpleNamespace())
        )
        runtime._submission_initialized_agent_runs = {}
        runtime._submission_selector_facts = {"task-1": {"stale": True}}
        runtime._submission_file_selection_computations = {
            "task-1": SimpleNamespace(triggered=False)
        }
        context = PreparedAgentRecoveryContext(
            username="alice",
            current_user_input="hello",
            initial_required_tool_name=None,
            model_options={},
            bundle_revisions={},
            execution_metadata={},
            memory_context=None,
            mcp_binding=None,
            mcp_assignment=None,
            available_mcp_servers=(),
        )

        await runtime.initialize_agent_handoff(
            _record(),
            {"upload_refs": [], "requested_capability_id": None},
            context,
        )

        self.assertNotIn("task-1", runtime._submission_selector_facts)
        self.assertNotIn(
            "task-1", runtime._submission_file_selection_computations
        )


if __name__ == "__main__":
    unittest.main()
