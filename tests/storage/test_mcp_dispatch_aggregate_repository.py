from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from src.core.enums import NodeStatus, TaskStatus, UserMCPHealthStatus, UserMCPTransport
from src.core.models import (
    Conversation,
    Interrupt,
    InterruptAnswer,
    MCPBranchRecord,
    MCPCallRecord,
    MCPDurableResultSnapshot,
    MCPMRTRRequestStateEvidence,
    MCPRemoteTaskBinding,
    MCPSealedState,
    MCPPendingActionPayloadSnapshot,
    MCPPendingToolAction,
    MCPPendingToolActionStatus,
    MCPTerminalCandidateSnapshot,
    MCPValidatedTerminalResultCandidate,
    MCPTerminalState,
    Task,
    TaskNode,
    UserMCPServer,
    UserMCPToolGrant,
)
from src.integrations.mcp.cp7_artifacts import (
    canonical_sha256,
    mcp_no_server_intent_id,
    mcp_terminal_candidate_id,
)
from src.integrations.master_key import MasterKeyDeriver, MasterKeyDomain
from src.integrations.mcp.pending_action_payloads import (
    MCPPendingActionPayloadCipher,
    MCPPendingActionPayloadIdentity,
    MCPPendingActionPayloadStore,
)
from src.storage.sqlite import (
    SQLiteStorage,
    bootstrap_sqlite_database,
    create_sqlite_engine,
    create_sqlite_session_factory,
)
from src.storage.sqlite.models import (
    MCPDispatchResumeOutboxRow,
    MCPDurableResultLifecycleRow,
    MCPPendingToolActionRow,
    MCPTerminalCandidateLifecycleRow,
    UserMCPToolGrantRow,
)


NOW = datetime(2026, 8, 18, 8, 0, 0)


class ExactPayloadReader:
    def __init__(self) -> None:
        self.replacement: MCPPendingActionPayloadSnapshot | None = None
        self.calls = 0

    def revalidate(
        self, snapshot: MCPPendingActionPayloadSnapshot
    ) -> MCPPendingActionPayloadSnapshot:
        self.calls += 1
        return self.replacement or snapshot


class ExactSnapshotReader:
    def __init__(self) -> None:
        self.replacement = None

    def revalidate(self, snapshot):
        return self.replacement or snapshot


class ExactMRTRReader:
    def __init__(self) -> None:
        self.evidence: MCPMRTRRequestStateEvidence | None = None

    def read(self, record, *, server_id: str, protocol_version: str):
        if self.evidence is None:
            raise RuntimeError("test MRTR evidence missing")
        if (
            record.sealed_state_ref != self.evidence.sealed_state_ref
            or server_id != "server-1"
            or protocol_version != "2026-07-28"
        ):
            raise RuntimeError("test MRTR evidence mismatch")
        return self.evidence


class MCPDispatchAggregateRepositoryTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.engine = create_sqlite_engine(
            Path(self.temporary.name) / "aggregate.sqlite3"
        )
        self.addCleanup(self.engine.dispose)
        bootstrap_sqlite_database(self.engine)
        self.sessions = create_sqlite_session_factory(self.engine)
        self.reader = ExactPayloadReader()
        self.candidate_reader = ExactSnapshotReader()
        self.result_reader = ExactSnapshotReader()
        self.mrtr_reader = ExactMRTRReader()
        self.storage = SQLiteStorage(
            self.sessions,
            mcp_pending_action_payload_reader=self.reader,
            mcp_terminal_candidate_snapshot_reader=self.candidate_reader,
            mcp_durable_result_snapshot_reader=self.result_reader,
            mcp_mrtr_request_state_evidence_reader=self.mrtr_reader,
        )
        self.task = Task(
            task_id="task-1",
            conversation_id="conversation-1",
            root_message_id="message-1",
            status=TaskStatus.RUNNING,
            mcp_execution_mode="user_scoped",
            mcp_shadow_enabled=False,
            mcp_rollout_config_version="cp7",
            mcp_route_reason_code="enforce_selected",
            mcp_rollout_mode="enforce",
            created_at=NOW,
            updated_at=NOW,
        )
        self.node = TaskNode(
            node_id="node-1",
            task_id=self.task.task_id,
            capability_id="mcp.dispatch",
            status=NodeStatus.RUNNING,
            started_at=NOW,
        )
        self.server = UserMCPServer(
            server_id="server-1",
            owner_user_id="alice",
            display_name="Server",
            routing_description="aggregate",
            endpoint_url="https://example.test/mcp",
            transport=UserMCPTransport.STREAMABLE_HTTP,
            health_status=UserMCPHealthStatus.AVAILABLE,
            created_at=NOW,
            updated_at=NOW,
        )
        await self.storage.save_conversation(
            Conversation(self.task.conversation_id, "alice")
        )
        await self.storage.save_task(self.task)
        await self.storage.save_task_node(self.node)
        await self.storage.create_user_mcp_server(self.server)
        await self.storage.save_mcp_branch_record(
            MCPBranchRecord(
                branch_id="branch-1",
                owner_user_id="alice",
                task_id=self.task.task_id,
                node_id=self.node.node_id,
                status="running",
                initial_server_id=self.server.server_id,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        await self.storage.save_user_mcp_tool_grant(
            UserMCPToolGrant(
                grant_id="grant-1",
                owner_user_id="alice",
                server_id=self.server.server_id,
                tool_name="lookup",
                server_security_version=self.server.security_version,
                input_schema_sha256="sha256:schema",
                granted_at=NOW,
            )
        )
        await self.storage.arm_user_mcp_target_intent(
            self.task.task_id,
            self.node.node_id,
            self.server.server_id,
            {"task_id": self.task.task_id},
            NOW,
        )
        self.intent_id = mcp_no_server_intent_id(
            self.task.task_id, node_id=self.node.node_id
        )
        await self.storage.resolve_user_mcp_target_intent(self.intent_id, NOW)
        self.outbox_id = f"mcp-dispatch-resume:v1:{self.intent_id}"
        with self.sessions() as session:
            session.add(
                MCPPendingToolActionRow(
                    action_id="action-1",
                    owner_user_id="alice",
                    conversation_id=self.task.conversation_id,
                    task_id=self.task.task_id,
                    node_id=self.node.node_id,
                    server_id=self.server.server_id,
                    tool_name="lookup",
                    arguments_sha256="sha256:arguments",
                    approval_fingerprint="sha256:approval",
                    arguments_payload_ref="mcp-action-payload-1",
                    payload_file_sha256="sha256:file",
                    payload_size_bytes=123,
                    encryption_version=1,
                    server_config_version=self.server.config_version,
                    server_security_version=self.server.security_version,
                    input_schema_sha256="sha256:schema",
                    status="approved",
                    revision=1,
                    approved_at=NOW,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            session.commit()
        self.snapshot = MCPPendingActionPayloadSnapshot(
            action_id="action-1",
            owner_user_id="alice",
            task_id=self.task.task_id,
            node_id=self.node.node_id,
            server_id=self.server.server_id,
            tool_name="lookup",
            arguments_sha256="sha256:arguments",
            arguments_payload_ref="mcp-action-payload-1",
            payload_file_sha256="sha256:file",
            payload_size_bytes=123,
            encryption_version=1,
            server_config_version=self.server.config_version,
            server_security_version=self.server.security_version,
            input_schema_sha256="sha256:schema",
            file_device=1,
            file_inode=2,
            file_mode=0o600,
            file_owner_uid=os.getuid(),
        )

    async def _claim(self, *, owner: str = "worker", token: str = "token"):
        outbox = await self.storage.get_mcp_dispatch_resume_outbox(self.outbox_id)
        return await self.storage.claim_mcp_dispatch(
            self.outbox_id,
            owner,
            token,
            outbox.revision,
            NOW,
            NOW + timedelta(seconds=30),
        )

    def _call(
        self,
        call_ref: str = "call-1",
        *,
        call_sequence: int = 1,
        action_id: str = "action-1",
        arguments_sha256: str = "sha256:arguments",
    ) -> MCPCallRecord:
        return MCPCallRecord(
            call_ref=call_ref,
            branch_id="branch-1",
            owner_user_id="alice",
            task_id=self.task.task_id,
            node_id=self.node.node_id,
            server_id=self.server.server_id,
            tool_name="lookup",
            status="reserved",
            call_sequence=call_sequence,
            arguments_sha256=arguments_sha256,
            server_security_version=self.server.security_version,
            server_config_version=self.server.config_version,
            input_schema_sha256="sha256:schema",
            protocol_version="2026-07-28",
            pending_action_id=action_id,
            created_at=NOW,
            updated_at=NOW,
        )

    async def _admit(self):
        claimed = await self._claim()
        intent = await self.storage.get_mcp_no_server_intent(self.intent_id)
        admitted = await self.storage.admit_approved_mcp_action(
            self.intent_id,
            self.outbox_id,
            "action-1",
            intent.revision,
            claimed.revision,
            1,
            "worker",
            "token",
            self.snapshot,
            self._call(),
            NOW,
        )
        self.assertTrue(admitted)
        return await self.storage.get_mcp_dispatch_resume_outbox(self.outbox_id)

    def _approval_candidate(self):
        interrupt_id = "mcp-approval-action-1"
        action = MCPPendingToolAction(
            action_id="action-1",
            owner_user_id="alice",
            conversation_id=self.task.conversation_id,
            task_id=self.task.task_id,
            node_id=self.node.node_id,
            server_id=self.server.server_id,
            tool_name="lookup",
            arguments_sha256=self.snapshot.arguments_sha256,
            approval_fingerprint="sha256:approval",
            arguments_payload_ref=self.snapshot.arguments_payload_ref,
            payload_file_sha256=self.snapshot.payload_file_sha256,
            payload_size_bytes=self.snapshot.payload_size_bytes,
            encryption_version=self.snapshot.encryption_version,
            server_config_version=self.server.config_version,
            server_security_version=self.server.security_version,
            input_schema_sha256="sha256:schema",
            status=MCPPendingToolActionStatus.WAITING_APPROVAL,
            revision=0,
            created_at=NOW,
            updated_at=NOW,
            approval_interrupt_id=interrupt_id,
        )
        interrupt = Interrupt(
            interrupt_id=interrupt_id,
            conversation_id=self.task.conversation_id,
            task_id=self.task.task_id,
            node_id=self.node.node_id,
            source_agent="mcp.dispatch",
            source_message_id=self.task.root_message_id,
            question="Allow Server to call lookup?",
            reason_code="mcp_tool_approval_required",
            required_fields={
                "mcp_tool_approval": {
                    "type": "string",
                    "enum": ["allow_once", "always_allow", "deny"],
                },
                "approval_ref": action.approval_fingerprint,
                "server_id": action.server_id,
                "tool_name": action.tool_name,
            },
            created_at=NOW,
        )
        return action, interrupt

    async def _suspend_for_approval(self):
        with self.sessions() as session:
            action_row = session.get(MCPPendingToolActionRow, "action-1")
            grant_row = session.get(UserMCPToolGrantRow, "grant-1")
            if action_row is not None:
                session.delete(action_row)
            if grant_row is not None:
                session.delete(grant_row)
            session.commit()
        claimed = await self._claim()
        intent = await self.storage.get_mcp_no_server_intent(self.intent_id)
        action, interrupt = self._approval_candidate()
        suspended = await self.storage.suspend_mcp_for_approval(
            self.intent_id,
            self.outbox_id,
            intent.revision,
            claimed.revision,
            "worker",
            "token",
            action,
            interrupt,
            self.snapshot,
            NOW,
        )
        self.assertEqual(str(suspended), "suspended")
        return action, interrupt

    def _approval_answer(self, interrupt_id: str, decision: str, suffix: str = "1"):
        return InterruptAnswer(
            interrupt_answer_id=f"answer-{suffix}",
            interrupt_id=interrupt_id,
            answer_payload={"mcp_tool_approval": decision},
            source_message_id=f"message-answer-{suffix}",
            accepted=True,
            created_at=NOW + timedelta(seconds=1),
            accepted_at=NOW + timedelta(seconds=1),
        )

    async def test_latest_approved_action_read_is_closed_to_task_node_owner(self) -> None:
        action = await self.storage.get_latest_approved_mcp_tool_action(
            "alice", self.task.task_id, self.node.node_id
        )
        self.assertIsNotNone(action)
        self.assertEqual(action.action_id, "action-1")
        self.assertIsNone(
            await self.storage.get_latest_approved_mcp_tool_action(
                "other-owner", self.task.task_id, self.node.node_id
            )
        )

    async def test_suspend_and_allow_once_commit_action_interrupt_answer_and_cursor(self) -> None:
        action, interrupt = await self._suspend_for_approval()
        waiting_outbox = await self.storage.get_mcp_dispatch_resume_outbox(
            self.outbox_id
        )
        self.assertEqual(str(waiting_outbox.status), "waiting_approval")
        self.assertIsNone(waiting_outbox.claim_token)
        self.assertEqual(waiting_outbox.approval_round_total, 1)
        self.assertEqual(
            str((await self.storage.get_task_node(self.node.node_id)).status),
            "waiting_for_input",
        )
        self.assertEqual(
            (
                await self.storage.get_mcp_pending_tool_action_for_interrupt(
                    interrupt.interrupt_id
                )
            ).action_id,
            action.action_id,
        )

        accepted = await self.storage.accept_mcp_tool_approval(
            interrupt.interrupt_id,
            self._approval_answer(interrupt.interrupt_id, "allow_once"),
            "allow_once",
            NOW + timedelta(seconds=1),
        )

        self.assertEqual(str(accepted), "accepted")
        outbox = await self.storage.get_mcp_dispatch_resume_outbox(self.outbox_id)
        saved_action = await self.storage.get_mcp_pending_tool_action(action.action_id)
        saved_interrupt = await self.storage.get_interrupt(interrupt.interrupt_id)
        answers = await self.storage.list_interrupt_answers(interrupt.interrupt_id)
        self.assertEqual(str(outbox.status), "pending")
        self.assertEqual(str(outbox.resume_reason), "approval_accepted")
        self.assertEqual(outbox.resume_answer_id, "answer-1")
        self.assertEqual(str(saved_action.status), "approved")
        self.assertEqual(saved_action.accepted_answer_id, "answer-1")
        self.assertEqual(str(saved_interrupt.status), "answered")
        self.assertEqual(len(answers), 1)
        self.assertTrue(answers[0].accepted)
        self.assertEqual(
            str((await self.storage.get_task_node(self.node.node_id)).status),
            "ready_to_resume",
        )
    async def test_suspend_revalidates_real_held_payload_descriptor(self) -> None:
        with self.sessions() as session:
            session.delete(session.get(MCPPendingToolActionRow, "action-1"))
            session.delete(session.get(UserMCPToolGrantRow, "grant-1"))
            session.commit()
        payload_store = MCPPendingActionPayloadStore(
            Path(self.temporary.name) / "pending-action-payloads",
            cipher=MCPPendingActionPayloadCipher(
                MasterKeyDeriver.from_bytes(b"p" * 32).derive(
                    MasterKeyDomain.MCP_RECOVERY
                )
            ),
            disk_available=lambda _path: True,
        )
        real_storage = SQLiteStorage(
            self.sessions,
            mcp_pending_action_payload_reader=payload_store,
            mcp_terminal_candidate_snapshot_reader=self.candidate_reader,
            mcp_durable_result_snapshot_reader=self.result_reader,
        )
        identity = MCPPendingActionPayloadIdentity(
            action_id="action-1",
            owner_user_id="alice",
            task_id=self.task.task_id,
            node_id=self.node.node_id,
            server_id=self.server.server_id,
            tool_name="lookup",
            server_config_version=self.server.config_version,
            server_security_version=self.server.security_version,
            input_schema_sha256="sha256:schema",
            arguments_sha256="sha256:arguments",
        )
        snapshot = await payload_store.seal(identity, {"query": "alice"})
        action, interrupt = self._approval_candidate()
        action = replace(
            action,
            arguments_payload_ref=snapshot.arguments_payload_ref,
            payload_file_sha256=snapshot.payload_file_sha256,
            payload_size_bytes=snapshot.payload_size_bytes,
        )
        claimed = await real_storage.claim_mcp_dispatch(
            self.outbox_id,
            "worker",
            "token",
            (await real_storage.get_mcp_dispatch_resume_outbox(self.outbox_id)).revision,
            NOW,
            NOW + timedelta(seconds=30),
        )
        intent = await real_storage.get_mcp_no_server_intent(self.intent_id)

        async with payload_store.open_validated(
            identity,
            snapshot.arguments_payload_ref,
            expected_snapshot=snapshot,
        ) as opened:
            suspended = await real_storage.suspend_mcp_for_approval(
                self.intent_id,
                self.outbox_id,
                intent.revision,
                claimed.revision,
                "worker",
                "token",
                action,
                interrupt,
                opened.snapshot,
                NOW,
            )

        self.assertEqual(str(suspended), "suspended")

    async def test_always_allow_creates_grant_and_double_answer_is_single_winner(self) -> None:
        action, interrupt = await self._suspend_for_approval()
        results = await asyncio.gather(
            self.storage.accept_mcp_tool_approval(
                interrupt.interrupt_id,
                self._approval_answer(
                    interrupt.interrupt_id, "always_allow", "first"
                ),
                "always_allow",
                NOW + timedelta(seconds=1),
            ),
            self.storage.accept_mcp_tool_approval(
                interrupt.interrupt_id,
                self._approval_answer(
                    interrupt.interrupt_id, "always_allow", "second"
                ),
                "always_allow",
                NOW + timedelta(seconds=1),
            ),
        )

        self.assertEqual(
            {str(result) for result in results},
            {"accepted", "already_accepted"},
        )
        self.assertEqual(
            len(await self.storage.list_interrupt_answers(interrupt.interrupt_id)),
            1,
        )
        grant = await self.storage.get_valid_user_mcp_tool_grant(
            "alice",
            action.server_id,
            action.tool_name,
            server_security_version=action.server_security_version,
            input_schema_sha256=action.input_schema_sha256,
        )
        self.assertIsNotNone(grant)

    async def test_deny_finalizes_without_call_and_does_not_create_grant(self) -> None:
        action, interrupt = await self._suspend_for_approval()

        denied = await self.storage.accept_mcp_tool_approval(
            interrupt.interrupt_id,
            self._approval_answer(interrupt.interrupt_id, "deny"),
            "deny",
            NOW + timedelta(seconds=1),
        )

        self.assertEqual(str(denied), "denied_finalized")
        outbox = await self.storage.get_mcp_dispatch_resume_outbox(self.outbox_id)
        self.assertEqual(str(outbox.status), "aborted")
        self.assertEqual(outbox.completion_mode, "stopped_no_call")
        self.assertEqual(
            str(
                (
                    await self.storage.get_mcp_pending_tool_action(
                        action.action_id
                    )
                ).status
            ),
            "denied",
        )
        self.assertIsNone(
            await self.storage.get_valid_user_mcp_tool_grant(
                "alice",
                action.server_id,
                action.tool_name,
                server_security_version=action.server_security_version,
                input_schema_sha256=action.input_schema_sha256,
            )
        )

    async def test_version_drift_invalidates_approval_without_accepting_answer(self) -> None:
        action, interrupt = await self._suspend_for_approval()
        updated = await self.storage.update_user_mcp_server(
            "alice",
            self.server.server_id,
            changes={"routing_description": "changed"},
            security_sensitive=True,
            updated_at=NOW + timedelta(milliseconds=500),
        )
        self.assertGreater(updated.security_version, self.server.security_version)

        result = await self.storage.accept_mcp_tool_approval(
            interrupt.interrupt_id,
            self._approval_answer(interrupt.interrupt_id, "allow_once"),
            "allow_once",
            NOW + timedelta(seconds=1),
        )

        self.assertEqual(str(result), "invalidated")
        self.assertEqual(
            str(
                (
                    await self.storage.get_mcp_pending_tool_action(
                        action.action_id
                    )
                ).status
            ),
            "invalidated",
        )
        self.assertEqual(
            await self.storage.list_interrupt_answers(interrupt.interrupt_id), []
        )

    async def test_cancel_then_answer_cannot_resurrect_waiting_approval(self) -> None:
        action, interrupt = await self._suspend_for_approval()
        cancelled = await self.storage.cancel_mcp_dispatch(
            self.intent_id,
            self.outbox_id,
            self.node.node_id,
            NOW + timedelta(seconds=1),
        )
        result = await self.storage.accept_mcp_tool_approval(
            interrupt.interrupt_id,
            self._approval_answer(interrupt.interrupt_id, "allow_once"),
            "allow_once",
            NOW + timedelta(seconds=2),
        )

        self.assertEqual(str(cancelled), "finalized")
        self.assertEqual(str(result), "conflict")
        self.assertEqual(
            str((await self.storage.get_interrupt(interrupt.interrupt_id)).status),
            "cancelled",
        )
        self.assertEqual(
            str(
                (
                    await self.storage.get_mcp_pending_tool_action(
                        action.action_id
                    )
                ).status
            ),
            "invalidated",
        )

    def _terminal_snapshots(
        self,
        *,
        terminal_state: MCPTerminalState = MCPTerminalState.COMPLETED,
        call_ref: str = "call-1",
        result_ref: str = "mcp-result-1",
    ):
        result_payload_sha = canonical_sha256(
            {
                "safe_result_ref": result_ref,
                "terminal_state": str(terminal_state),
            }
        )
        completed = terminal_state is MCPTerminalState.COMPLETED
        candidate = MCPValidatedTerminalResultCandidate(
            candidate_id=mcp_terminal_candidate_id(
                call_ref, result_payload_sha
            ),
            owner_user_id="alice",
            conversation_id=self.task.conversation_id,
            task_id=self.task.task_id,
            node_id=self.node.node_id,
            intent_id=self.intent_id,
            call_id=call_ref,
            server_id=self.server.server_id,
            server_config_version=self.server.config_version,
            server_security_version=self.server.security_version,
            terminal_state=terminal_state,
            result_payload_sha256=result_payload_sha,
            safe_result_ref=result_ref if completed else None,
            safe_result_ref_sha256=(
                canonical_sha256(result_ref) if completed else None
            ),
            safe_error_code=None if completed else "remote_failed",
            sealed_at=NOW,
            safe_result_content_sha256=(
                "sha256:" + "a" * 64 if completed else None
            ),
            safe_result_size_bytes=2 if completed else None,
            safe_result_store_kind=(
                "durable_content_addressed" if completed else None
            ),
        )
        candidate_snapshot = MCPTerminalCandidateSnapshot(
            candidate=candidate,
            candidate_schema="maf.user_mcp.cp7.terminal_result_candidate.v2",
            active_candidate_filename=f"candidate-{call_ref}.json",
            active_task_index_filename=f"task-index-{call_ref}.json",
            active_call_index_filename=f"call-index-{call_ref}.json",
            candidate_file_sha256="sha256:" + "b" * 64,
            task_index_file_sha256="sha256:" + "c" * 64,
            call_index_file_sha256="sha256:" + "d" * 64,
        )
        result_snapshot = (
            MCPDurableResultSnapshot(
                result_ref=result_ref,
                owner_user_id="alice",
                task_id=self.task.task_id,
                node_id=self.node.node_id,
                call_id=call_ref,
                content_sha256="sha256:" + "a" * 64,
                size_bytes=2,
                store_kind="durable_content_addressed",
                data_filename=f"{result_ref}.json",
                manifest_filename=f"{result_ref}.manifest.json",
                data_file_sha256="sha256:" + "a" * 64,
                manifest_file_sha256="sha256:" + "e" * 64,
                data_file_device=1,
                data_file_inode=2,
                data_file_mode=0o600,
                data_file_owner_uid=os.getuid(),
                manifest_file_device=1,
                manifest_file_inode=3,
                manifest_file_mode=0o600,
                manifest_file_owner_uid=os.getuid(),
            )
            if completed
            else None
        )
        return candidate_snapshot, result_snapshot

    async def test_claim_renew_and_competing_claim_are_revision_guarded(self) -> None:
        claimed = await self._claim()
        self.assertEqual(str(claimed.status), "claimed")
        self.assertIsNone(
            await self.storage.claim_mcp_dispatch(
                self.outbox_id,
                "other",
                "other-token",
                claimed.revision,
                NOW,
                NOW + timedelta(seconds=30),
            )
        )
        renewed = await self.storage.renew_mcp_dispatch_claim(
            self.outbox_id,
            "worker",
            "token",
            claimed.revision,
            NOW + timedelta(seconds=10),
            NOW + timedelta(seconds=40),
        )
        self.assertIsNotNone(renewed)
        self.assertEqual(renewed.lease_expires_at, NOW + timedelta(seconds=40))
        self.assertIsNone(
            await self.storage.renew_mcp_dispatch_claim(
                self.outbox_id,
                "worker",
                "token",
                renewed.revision,
                NOW + timedelta(seconds=21),
                NOW + timedelta(seconds=51),
            )
        )

    async def test_expired_unadmitted_claim_returns_to_pending(self) -> None:
        claimed = await self._claim()
        recovered = await self.storage.release_or_recover_mcp_dispatch_claim(
            self.outbox_id,
            claimed.revision,
            NOW + timedelta(seconds=31),
        )
        self.assertEqual(str(recovered.status), "pending")
        self.assertIsNone(recovered.claim_token)

    async def test_selector_step_budget_is_durable_and_capped_at_sixty_four(self) -> None:
        current = await self._claim()
        for _ in range(64):
            current = await self.storage.consume_mcp_dispatch_selector_step(
                self.outbox_id,
                "worker",
                "token",
                current.revision,
                NOW,
            )
            self.assertIsNotNone(current)

        rejected = await self.storage.consume_mcp_dispatch_selector_step(
            self.outbox_id,
            "worker",
            "token",
            current.revision,
            NOW,
        )

        self.assertIsNone(rejected)
        persisted = await self.storage.get_mcp_dispatch_resume_outbox(
            self.outbox_id
        )
        self.assertEqual(persisted.selector_step_total, 64)

    async def test_twentieth_completed_approval_round_blocks_another_suspend(self) -> None:
        with self.sessions() as session:
            session.delete(session.get(MCPPendingToolActionRow, "action-1"))
            session.delete(session.get(UserMCPToolGrantRow, "grant-1"))
            outbox = session.get(MCPDispatchResumeOutboxRow, self.outbox_id)
            outbox.approval_round_total = 20
            session.commit()
        claimed = await self._claim()
        intent = await self.storage.get_mcp_no_server_intent(self.intent_id)
        action, interrupt = self._approval_candidate()

        suspended = await self.storage.suspend_mcp_for_approval(
            self.intent_id,
            self.outbox_id,
            intent.revision,
            claimed.revision,
            "worker",
            "token",
            action,
            interrupt,
            self.snapshot,
            NOW,
        )

        self.assertEqual(str(suspended), "conflict")
        self.assertIsNone(
            await self.storage.get_mcp_pending_tool_action(action.action_id)
        )

    async def test_admission_atomically_consumes_action_and_opens_network_gate(self) -> None:
        claimed = await self._claim()
        intent = await self.storage.get_mcp_no_server_intent(self.intent_id)

        admitted = await self.storage.admit_approved_mcp_action(
            self.intent_id,
            self.outbox_id,
            "action-1",
            intent.revision,
            claimed.revision,
            1,
            "worker",
            "token",
            self.snapshot,
            self._call(),
            NOW,
        )

        self.assertTrue(admitted)
        action = await self.storage.get_mcp_pending_tool_action("action-1")
        call = await self.storage.get_mcp_call_record(
            "alice", self.task.task_id, "call-1"
        )
        outbox = await self.storage.get_mcp_dispatch_resume_outbox(self.outbox_id)
        intent = await self.storage.get_mcp_no_server_intent(self.intent_id)
        self.assertEqual(str(action.status), "consumed")
        self.assertEqual(action.consumed_at, NOW)
        self.assertTrue(call.may_have_dispatched)
        self.assertEqual(call.pending_action_id, "action-1")
        self.assertEqual(str(outbox.status), "active")
        self.assertEqual(outbox.claim_token, "token")
        self.assertEqual(str(intent.status), "dispatched")
        self.assertEqual(self.reader.calls, 1)

    async def test_valid_grant_admits_exact_action_candidate_without_prior_row(self) -> None:
        with self.sessions() as session:
            session.delete(session.get(MCPPendingToolActionRow, "action-1"))
            session.commit()
        candidate, _interrupt = self._approval_candidate()
        candidate = replace(
            candidate,
            status=MCPPendingToolActionStatus.APPROVED,
            approval_interrupt_id=None,
            approved_at=NOW,
        )
        claimed = await self._claim()
        intent = await self.storage.get_mcp_no_server_intent(self.intent_id)

        admitted = await self.storage.admit_approved_mcp_action(
            self.intent_id,
            self.outbox_id,
            candidate.action_id,
            intent.revision,
            claimed.revision,
            candidate.revision,
            "worker",
            "token",
            self.snapshot,
            self._call(),
            NOW,
            action_candidate=candidate,
        )

        self.assertTrue(admitted)
        saved = await self.storage.get_mcp_pending_tool_action(candidate.action_id)
        self.assertEqual(str(saved.status), "consumed")
        self.assertEqual(saved.revision, 1)
        self.assertEqual(saved.approved_at, NOW)

    async def test_mrtr_suspend_and_answer_commit_call_interrupt_and_cursor_atomically(
        self,
    ) -> None:
        await self._admit()
        sealed_ref = "mcp-request-state:test"
        await self.storage.save_mcp_sealed_state(
            MCPSealedState(
                sealed_state_ref=sealed_ref,
                owner_user_id="alice",
                task_id=self.task.task_id,
                node_id=self.node.node_id,
                call_ref="call-1",
                state_kind="request_state",
                ciphertext=b"ciphertext",
                nonce=b"n" * 12,
                encryption_version=1,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        self.mrtr_reader.evidence = MCPMRTRRequestStateEvidence(
            sealed_state_ref=sealed_ref,
            owner_user_id="alice",
            task_id=self.task.task_id,
            node_id=self.node.node_id,
            call_ref="call-1",
            request_state="opaque",
            tool_name="lookup",
            arguments_sha256="sha256:arguments",
            input_requests={"confirm": {"type": "boolean"}},
            pending_action_id="action-1",
            arguments_payload_ref="mcp-action-payload-1",
        )
        interrupt = Interrupt(
            interrupt_id="mcp-input-call-1",
            conversation_id=self.task.conversation_id,
            task_id=self.task.task_id,
            node_id=self.node.node_id,
            source_agent="mcp.dispatch",
            source_message_id=self.task.root_message_id,
            question="Input required",
            reason_code="mcp_input_required",
            required_fields={
                "mcp_input_responses": {"type": "object"},
                "sealed_request_state_ref": sealed_ref,
                "server_id": self.server.server_id,
                "tool_name": "lookup",
            },
            created_at=NOW,
        )
        intent = await self.storage.get_mcp_no_server_intent(self.intent_id)
        outbox = await self.storage.get_mcp_dispatch_resume_outbox(self.outbox_id)

        suspended = await self.storage.suspend_mcp_for_input(
            self.intent_id,
            self.outbox_id,
            "call-1",
            sealed_ref,
            intent.revision,
            outbox.revision,
            "worker",
            "token",
            interrupt,
            NOW + timedelta(seconds=1),
        )

        self.assertEqual(str(suspended), "suspended")
        self.assertEqual(
            (await self.storage.get_mcp_call_record("alice", self.task.task_id, "call-1")).status,
            "input_required",
        )
        self.assertEqual(
            str((await self.storage.get_mcp_dispatch_resume_outbox(self.outbox_id)).status),
            "waiting_input",
        )
        self.assertEqual(
            str((await self.storage.get_task_node(self.node.node_id)).status),
            "waiting_for_input",
        )
        answer = InterruptAnswer(
            interrupt_answer_id="answer-mrtr-1",
            interrupt_id=interrupt.interrupt_id,
            answer_payload={"mcp_input_responses": {"confirm": True}},
            source_message_id="message-answer-mrtr-1",
            accepted=True,
            created_at=NOW + timedelta(seconds=2),
            accepted_at=NOW + timedelta(seconds=2),
        )

        accepted = await self.storage.accept_mcp_mrtr_answer(
            interrupt.interrupt_id,
            answer,
            NOW + timedelta(seconds=2),
        )

        self.assertEqual(str(accepted), "accepted")
        resumed = await self.storage.get_mcp_dispatch_resume_outbox(self.outbox_id)
        self.assertEqual(str(resumed.status), "pending")
        self.assertEqual(str(resumed.resume_reason), "mrtr_answer")
        self.assertIsNone(resumed.resume_receipt_id)
        self.assertEqual(resumed.resume_answer_id, answer.interrupt_answer_id)
        self.assertEqual(
            str((await self.storage.get_task_node(self.node.node_id)).status),
            "ready_to_resume",
        )
        claimed = await self.storage.claim_mcp_dispatch(
            self.outbox_id,
            "worker",
            "token-2",
            resumed.revision,
            NOW + timedelta(seconds=3),
            NOW + timedelta(seconds=33),
        )
        intent = await self.storage.get_mcp_no_server_intent(self.intent_id)
        continuation = replace(
            self._call("call-2", call_sequence=2),
            pending_action_id=None,
            continuation_of_call_ref="call-1",
            created_at=NOW + timedelta(seconds=3),
            updated_at=NOW + timedelta(seconds=3),
        )

        admitted = await self.storage.admit_mrtr_continuation(
            self.intent_id,
            self.outbox_id,
            "call-1",
            sealed_ref,
            answer.interrupt_answer_id,
            intent.revision,
            claimed.revision,
            "worker",
            "token-2",
            self.snapshot,
            continuation,
            NOW + timedelta(seconds=3),
        )

        self.assertTrue(admitted)
        saved_continuation = await self.storage.get_mcp_call_record(
            "alice", self.task.task_id, "call-2"
        )
        self.assertEqual(saved_continuation.status, "active")
        self.assertEqual(saved_continuation.continuation_of_call_ref, "call-1")
        self.assertIsNone(saved_continuation.pending_action_id)
        self.assertEqual(
            str((await self.storage.get_mcp_pending_tool_action("action-1")).status),
            "consumed",
        )

    async def test_remote_task_publication_adopts_binding_and_releases_dispatch_claim(
        self,
    ) -> None:
        active = await self._admit()
        binding = MCPRemoteTaskBinding(
            safe_remote_task_ref="mcp-task:remote-1",
            owner_user_id="alice",
            task_id=self.task.task_id,
            node_id=self.node.node_id,
            call_ref="call-1",
            server_id=self.server.server_id,
            protocol_version="2026-07-28",
            remote_task_ciphertext=b"ciphertext",
            remote_task_nonce=b"n" * 12,
            encryption_version=1,
            last_status="working",
            next_poll_at=None,
            created_at=NOW,
            updated_at=NOW,
        )
        await self.storage.save_mcp_remote_task_binding(binding)
        intent = await self.storage.get_mcp_no_server_intent(self.intent_id)

        published = await self.storage.publish_mcp_remote_task(
            self.intent_id,
            self.outbox_id,
            "call-1",
            binding.safe_remote_task_ref,
            intent.revision,
            active.revision,
            "worker",
            "token",
            NOW + timedelta(seconds=1),
        )

        self.assertIsNotNone(published)
        self.assertEqual(published.published_at, NOW + timedelta(seconds=1))
        call = await self.storage.get_mcp_call_record(
            "alice", self.task.task_id, "call-1"
        )
        outbox = await self.storage.get_mcp_dispatch_resume_outbox(self.outbox_id)
        branch = await self.storage.get_mcp_branch_record(
            "alice", self.task.task_id, "branch-1"
        )
        self.assertEqual(call.status, "remote_pending")
        self.assertEqual(call.result_ref, binding.safe_remote_task_ref)
        self.assertEqual(str(outbox.status), "remote_pending")
        self.assertIsNone(outbox.claim_token)
        self.assertEqual(branch.active_call_ref, "call-1")
        self.assertEqual(branch.status, "waiting_for_dependency")
        self.assertEqual(
            str((await self.storage.get_task_node(self.node.node_id)).status),
            "waiting_for_dependency",
        )
        claimed_bindings = await self.storage.claim_due_mcp_remote_task_bindings(
            claim_owner="remote-worker",
            claim_token="remote-token",
            now=NOW + timedelta(seconds=2),
            lease_expires_at=NOW + timedelta(seconds=32),
        )
        self.assertEqual(len(claimed_bindings), 1)
        claimed_binding = claimed_bindings[0]
        candidate_snapshot, result_snapshot = self._terminal_snapshots()
        remote_outbox = await self.storage.get_mcp_dispatch_resume_outbox(
            self.outbox_id
        )

        committed = await self.storage.commit_mcp_call_terminal(
            "call-1",
            candidate_snapshot.candidate.candidate_id,
            self.outbox_id,
            remote_outbox.revision,
            None,
            None,
            candidate_snapshot,
            result_snapshot,
            NOW + timedelta(seconds=3),
            remote_binding_ref=binding.safe_remote_task_ref,
            remote_claim_owner="remote-worker",
            remote_claim_token="remote-token",
            remote_expected_revision=claimed_binding.revision,
        )

        self.assertEqual(str(committed), "committed_normal")
        terminal_binding = await self.storage.get_mcp_remote_task_binding(
            "alice", self.task.task_id, binding.safe_remote_task_ref
        )
        resumed_outbox = await self.storage.get_mcp_dispatch_resume_outbox(
            self.outbox_id
        )
        self.assertEqual(terminal_binding.last_status, "completed")
        self.assertIsNotNone(terminal_binding.terminal_at)
        self.assertEqual(str(resumed_outbox.status), "pending")
        self.assertEqual(str(resumed_outbox.resume_reason), "remote_terminal")
        self.assertEqual(
            str((await self.storage.get_task_node(self.node.node_id)).status),
            "ready_to_resume",
        )
        idempotent_finish = await self.storage.finish_mcp_remote_task_binding(
            "alice",
            self.task.task_id,
            binding.safe_remote_task_ref,
            claim_owner="remote-worker",
            claim_token="remote-token",
            expected_revision=claimed_binding.revision,
            remote_status="completed",
            call_status="completed",
            terminal_at=NOW + timedelta(seconds=3),
            result_ref=candidate_snapshot.candidate.safe_result_ref,
            result_receipt_id=resumed_outbox.result_receipt_id,
        )
        self.assertIsNotNone(idempotent_finish)

    async def test_expired_active_claim_with_unreceipted_call_cannot_recover_pending(
        self,
    ) -> None:
        claimed = await self._claim()
        intent = await self.storage.get_mcp_no_server_intent(self.intent_id)
        self.assertTrue(
            await self.storage.admit_approved_mcp_action(
                self.intent_id,
                self.outbox_id,
                "action-1",
                intent.revision,
                claimed.revision,
                1,
                "worker",
                "token",
                self.snapshot,
                self._call(),
                NOW,
            )
        )
        active = await self.storage.get_mcp_dispatch_resume_outbox(self.outbox_id)

        self.assertIsNone(
            await self.storage.release_or_recover_mcp_dispatch_claim(
                self.outbox_id,
                active.revision,
                NOW + timedelta(seconds=31),
            )
        )
        self.assertEqual(
            str(
                (
                    await self.storage.get_mcp_dispatch_resume_outbox(
                        self.outbox_id
                    )
                ).status
            ),
            "active",
        )

    async def test_startup_recovers_sealed_candidate_after_expired_dispatch_claim(
        self,
    ) -> None:
        await self._admit()
        candidate_snapshot, result_snapshot = self._terminal_snapshots()

        recovered = await self.storage.recover_mcp_terminal_candidate(
            candidate_snapshot,
            result_snapshot,
            NOW + timedelta(seconds=31),
        )

        self.assertEqual(str(recovered), "committed_normal")
        call = await self.storage.get_mcp_call_record(
            "alice", self.task.task_id, "call-1"
        )
        outbox = await self.storage.get_mcp_dispatch_resume_outbox(self.outbox_id)
        self.assertEqual(call.status, "completed")
        self.assertEqual(str(outbox.status), "active")
        self.assertEqual(outbox.claim_owner, "mcp-aggregate-startup")
        self.assertIsNotNone(
            await self.storage.get_mcp_terminal_result_receipt_for_call("call-1")
        )

    async def test_completed_terminal_commit_keeps_dispatch_active_until_finalizer(
        self,
    ) -> None:
        active = await self._admit()
        candidate_snapshot, result_snapshot = self._terminal_snapshots()
        committed_at = NOW + timedelta(seconds=1)

        committed = await self.storage.commit_mcp_call_terminal(
            "call-1",
            candidate_snapshot.candidate.candidate_id,
            self.outbox_id,
            active.revision,
            "worker",
            "token",
            candidate_snapshot,
            result_snapshot,
            committed_at,
        )

        self.assertEqual(str(committed), "committed_normal")
        outbox = await self.storage.get_mcp_dispatch_resume_outbox(self.outbox_id)
        call = await self.storage.get_mcp_call_record(
            "alice", self.task.task_id, "call-1"
        )
        self.assertEqual(str(outbox.status), "active")
        self.assertEqual(str(outbox.resume_reason), "ordinary_terminal")
        self.assertEqual(outbox.resume_receipt_id, outbox.result_receipt_id)
        self.assertEqual(call.status, "completed")
        with self.sessions() as session:
            candidate_lifecycle = session.get(
                MCPTerminalCandidateLifecycleRow,
                candidate_snapshot.candidate.candidate_id,
            )
            result_lifecycle = session.get(
                MCPDurableResultLifecycleRow, "mcp-result-1"
            )
            self.assertEqual(candidate_lifecycle.status, "retained")
            self.assertEqual(candidate_lifecycle.eligible_at, committed_at)
            self.assertEqual(result_lifecycle.status, "retained")
            self.assertIsNone(result_lifecycle.eligible_at)

        retry = await self.storage.commit_mcp_call_terminal(
            "call-1",
            candidate_snapshot.candidate.candidate_id,
            self.outbox_id,
            active.revision,
            "worker",
            "token",
            candidate_snapshot,
            result_snapshot,
            committed_at + timedelta(minutes=5),
        )
        self.assertEqual(str(retry), "already_committed")

        finalized = await self.storage.finalize_mcp_dispatch(
            self.intent_id,
            self.outbox_id,
            self.node.node_id,
            "completed",
            None,
            outbox.revision,
            "worker",
            "token",
            committed_at + timedelta(seconds=1),
        )
        self.assertEqual(str(finalized), "finalized")
        terminal_outbox = await self.storage.get_mcp_dispatch_resume_outbox(
            self.outbox_id
        )
        self.assertEqual(str(terminal_outbox.status), "completed")
        self.assertEqual(terminal_outbox.completion_mode, "completed")
        with self.sessions() as session:
            result_lifecycle = session.get(
                MCPDurableResultLifecycleRow, "mcp-result-1"
            )
            self.assertEqual(
                result_lifecycle.eligible_at,
                committed_at + timedelta(seconds=1, hours=24),
            )
        retry_after_finalize = await self.storage.commit_mcp_call_terminal(
            "call-1",
            candidate_snapshot.candidate.candidate_id,
            self.outbox_id,
            active.revision,
            None,
            None,
            candidate_snapshot,
            result_snapshot,
            committed_at + timedelta(minutes=10),
        )
        self.assertEqual(str(retry_after_finalize), "already_committed")

    async def test_failed_terminal_commit_atomically_finalizes_dispatch(self) -> None:
        active = await self._admit()
        candidate_snapshot, result_snapshot = self._terminal_snapshots(
            terminal_state=MCPTerminalState.FAILED
        )

        committed = await self.storage.commit_mcp_call_terminal(
            "call-1",
            candidate_snapshot.candidate.candidate_id,
            self.outbox_id,
            active.revision,
            "worker",
            "token",
            candidate_snapshot,
            result_snapshot,
            NOW + timedelta(seconds=1),
        )

        self.assertEqual(str(committed), "committed_normal")
        outbox = await self.storage.get_mcp_dispatch_resume_outbox(self.outbox_id)
        self.assertEqual(str(outbox.status), "completed")
        self.assertEqual(outbox.completion_mode, "failed_after_call")
        self.assertEqual(
            str((await self.storage.get_task(self.task.task_id)).status), "failed"
        )
        self.assertEqual(
            str((await self.storage.get_task_node(self.node.node_id)).status),
            "failed",
        )

    async def test_two_completed_calls_keep_dispatch_active_until_one_finalizer(self) -> None:
        first_active = await self._admit()
        first_candidate, first_result = self._terminal_snapshots()
        first_commit = await self.storage.commit_mcp_call_terminal(
            "call-1",
            first_candidate.candidate.candidate_id,
            self.outbox_id,
            first_active.revision,
            "worker",
            "token",
            first_candidate,
            first_result,
            NOW + timedelta(seconds=1),
        )
        self.assertEqual(str(first_commit), "committed_normal")

        with self.sessions() as session:
            session.add(
                MCPPendingToolActionRow(
                    action_id="action-2",
                    owner_user_id="alice",
                    conversation_id=self.task.conversation_id,
                    task_id=self.task.task_id,
                    node_id=self.node.node_id,
                    server_id=self.server.server_id,
                    tool_name="lookup",
                    arguments_sha256="sha256:arguments-2",
                    approval_fingerprint="sha256:approval-2",
                    arguments_payload_ref="mcp-action-payload-2",
                    payload_file_sha256="sha256:file-2",
                    payload_size_bytes=124,
                    encryption_version=1,
                    server_config_version=self.server.config_version,
                    server_security_version=self.server.security_version,
                    input_schema_sha256="sha256:schema",
                    status="approved",
                    revision=1,
                    approved_at=NOW + timedelta(seconds=1),
                    created_at=NOW + timedelta(seconds=1),
                    updated_at=NOW + timedelta(seconds=1),
                )
            )
            session.commit()
        second_snapshot = replace(
            self.snapshot,
            action_id="action-2",
            arguments_sha256="sha256:arguments-2",
            arguments_payload_ref="mcp-action-payload-2",
            payload_file_sha256="sha256:file-2",
            payload_size_bytes=124,
            file_inode=4,
        )
        intent = await self.storage.get_mcp_no_server_intent(self.intent_id)
        outbox = await self.storage.get_mcp_dispatch_resume_outbox(self.outbox_id)
        admitted = await self.storage.admit_approved_mcp_action(
            self.intent_id,
            self.outbox_id,
            "action-2",
            intent.revision,
            outbox.revision,
            1,
            "worker",
            "token",
            second_snapshot,
            self._call(
                "call-2",
                call_sequence=2,
                action_id="action-2",
                arguments_sha256="sha256:arguments-2",
            ),
            NOW + timedelta(seconds=2),
        )
        self.assertTrue(admitted)

        second_active = await self.storage.get_mcp_dispatch_resume_outbox(
            self.outbox_id
        )
        second_candidate, second_result = self._terminal_snapshots(
            call_ref="call-2", result_ref="mcp-result-2"
        )
        second_commit = await self.storage.commit_mcp_call_terminal(
            "call-2",
            second_candidate.candidate.candidate_id,
            self.outbox_id,
            second_active.revision,
            "worker",
            "token",
            second_candidate,
            second_result,
            NOW + timedelta(seconds=3),
        )
        self.assertEqual(str(second_commit), "committed_normal")
        before_finalize = await self.storage.get_mcp_dispatch_resume_outbox(
            self.outbox_id
        )
        self.assertEqual(str(before_finalize.status), "active")
        self.assertEqual(before_finalize.result_receipt_id, before_finalize.resume_receipt_id)
        self.assertEqual(
            len(
                await self.storage.list_mcp_call_records(
                    "alice", self.task.task_id, branch_id="branch-1"
                )
            ),
            2,
        )

        finalized = await self.storage.finalize_mcp_dispatch(
            self.intent_id,
            self.outbox_id,
            self.node.node_id,
            "completed",
            None,
            before_finalize.revision,
            "worker",
            "token",
            NOW + timedelta(seconds=4),
        )
        self.assertEqual(str(finalized), "finalized")
        self.assertEqual(
            str(
                (
                    await self.storage.get_mcp_dispatch_resume_outbox(
                        self.outbox_id
                    )
                ).status
            ),
            "completed",
        )

    async def test_no_call_stop_finalizer_aborts_and_invalidates_action(self) -> None:
        claimed = await self._claim()

        finalized = await self.storage.finalize_mcp_dispatch(
            self.intent_id,
            self.outbox_id,
            self.node.node_id,
            "stopped",
            None,
            claimed.revision,
            "worker",
            "token",
            NOW + timedelta(seconds=1),
        )

        self.assertEqual(str(finalized), "finalized")
        outbox = await self.storage.get_mcp_dispatch_resume_outbox(self.outbox_id)
        action = await self.storage.get_mcp_pending_tool_action("action-1")
        self.assertEqual(str(outbox.status), "aborted")
        self.assertEqual(outbox.completion_mode, "stopped_no_call")
        self.assertEqual(str(action.status), "invalidated")
        self.assertEqual(
            str((await self.storage.get_task_node(self.node.node_id)).status),
            "completed",
        )

    async def test_terminal_snapshot_drift_rolls_back_receipt_and_call(self) -> None:
        active = await self._admit()
        candidate_snapshot, result_snapshot = self._terminal_snapshots()
        self.result_reader.replacement = replace(
            result_snapshot, manifest_file_inode=99
        )

        with self.assertRaisesRegex(
            RuntimeError, "mcp_durable_result_snapshot_conflict"
        ):
            await self.storage.commit_mcp_call_terminal(
                "call-1",
                candidate_snapshot.candidate.candidate_id,
                self.outbox_id,
                active.revision,
                "worker",
                "token",
                candidate_snapshot,
                result_snapshot,
                NOW + timedelta(seconds=1),
            )
        self.assertIsNone(
            await self.storage.get_mcp_terminal_result_receipt_for_call("call-1")
        )
        self.assertEqual(
            (
                await self.storage.get_mcp_call_record(
                    "alice", self.task.task_id, "call-1"
                )
            ).status,
            "active",
        )

    async def test_cancel_after_admission_without_receipt_converges_unknown(self) -> None:
        await self._admit()

        cancelled = await self.storage.cancel_mcp_dispatch(
            self.intent_id,
            self.outbox_id,
            self.node.node_id,
            NOW + timedelta(seconds=1),
        )

        self.assertEqual(str(cancelled), "finalized")
        outbox = await self.storage.get_mcp_dispatch_resume_outbox(self.outbox_id)
        call = await self.storage.get_mcp_call_record(
            "alice", self.task.task_id, "call-1"
        )
        self.assertEqual(outbox.completion_mode, "unknown_no_replay")
        self.assertEqual(call.status, "unknown")
        self.assertEqual(
            str((await self.storage.get_task(self.task.task_id)).status), "failed"
        )

    async def test_payload_identity_drift_rolls_back_without_call(self) -> None:
        claimed = await self._claim()
        intent = await self.storage.get_mcp_no_server_intent(self.intent_id)
        self.reader.replacement = replace(
            self.snapshot, payload_file_sha256="sha256:drift"
        )

        with self.assertRaisesRegex(
            RuntimeError, "mcp_pending_action_payload_binding_conflict"
        ):
            await self.storage.admit_approved_mcp_action(
                self.intent_id,
                self.outbox_id,
                "action-1",
                intent.revision,
                claimed.revision,
                1,
                "worker",
                "token",
                self.snapshot,
                self._call(),
                NOW,
            )

        self.assertIsNone(
            await self.storage.get_mcp_call_record(
                "alice", self.task.task_id, "call-1"
            )
        )
        self.assertEqual(
            str((await self.storage.get_mcp_pending_tool_action("action-1")).status),
            "approved",
        )

    async def test_missing_payload_reader_fails_closed_without_call(self) -> None:
        storage = SQLiteStorage(self.sessions)
        claimed = await self._claim()
        intent = await self.storage.get_mcp_no_server_intent(self.intent_id)

        with self.assertRaisesRegex(
            RuntimeError, "mcp_pending_action_payload_reader_unavailable"
        ):
            await storage.admit_approved_mcp_action(
                self.intent_id,
                self.outbox_id,
                "action-1",
                intent.revision,
                claimed.revision,
                1,
                "worker",
                "token",
                self.snapshot,
                self._call(),
                NOW,
            )
        self.assertIsNone(
            await storage.get_mcp_call_record(
                "alice", self.task.task_id, "call-1"
            )
        )

    async def test_cancelled_task_or_deleted_server_cannot_admit(self) -> None:
        claimed = await self._claim()
        intent = await self.storage.get_mcp_no_server_intent(self.intent_id)
        await self.storage.compare_and_set_task(
            replace(
                self.task,
                cancel_requested_at=NOW,
                updated_at=NOW,
            ),
            expected_from_status=TaskStatus.RUNNING,
        )
        self.assertFalse(
            await self.storage.admit_approved_mcp_action(
                self.intent_id,
                self.outbox_id,
                "action-1",
                intent.revision,
                claimed.revision,
                1,
                "worker",
                "token",
                self.snapshot,
                self._call(),
                NOW,
            )
        )
        self.assertIsNone(
            await self.storage.get_mcp_call_record(
                "alice", self.task.task_id, "call-1"
            )
        )

    async def test_server_deletion_before_admission_blocks_network_gate(self) -> None:
        claimed = await self._claim()
        intent = await self.storage.get_mcp_no_server_intent(self.intent_id)
        await self.storage.mark_user_mcp_server_deleted(
            "alice", self.server.server_id, deleted_at=NOW
        )

        self.assertFalse(
            await self.storage.admit_approved_mcp_action(
                self.intent_id,
                self.outbox_id,
                "action-1",
                intent.revision,
                claimed.revision,
                1,
                "worker",
                "token",
                self.snapshot,
                self._call(),
                NOW,
            )
        )
        self.assertIsNone(
            await self.storage.get_mcp_call_record(
                "alice", self.task.task_id, "call-1"
            )
        )

    async def test_concurrent_admission_has_one_winner(self) -> None:
        claimed = await self._claim()
        intent = await self.storage.get_mcp_no_server_intent(self.intent_id)

        async def admit() -> bool:
            return await self.storage.admit_approved_mcp_action(
                self.intent_id,
                self.outbox_id,
                "action-1",
                intent.revision,
                claimed.revision,
                1,
                "worker",
                "token",
                self.snapshot,
                self._call(),
                NOW,
            )

        results = await asyncio.gather(admit(), admit())
        self.assertEqual(sorted(results), [False, True])
        calls = await self.storage.list_mcp_call_records(
            "alice", self.task.task_id
        )
        self.assertEqual(len(calls), 1)
