from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from src.core.models import MCPValidatedTerminalResultCandidate, MCPTerminalState
from src.integrations.mcp.cp7_artifacts import canonical_sha256, mcp_terminal_candidate_id
from src.storage.sqlite.bootstrap import bootstrap_sqlite_database
from src.storage.sqlite.models import (
    ConversationRow,
    EventRecordRow,
    MCPCallRecordRow,
    MCPDispatchResumeOutboxRow,
    MCPNoServerIntentRow,
    TaskNodeRow,
    TaskRow,
)
from src.storage.sqlite.repositories import SQLiteStorage


class UserMCPTerminalProjectionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.engine = create_engine(
            f"sqlite+pysqlite:///{Path(self.temp_dir.name) / 'state.db'}"
        )
        bootstrap_sqlite_database(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.at = datetime(2026, 8, 13, 4, 0)
        digest = canonical_sha256({"safe": "result"})
        self.candidate = MCPValidatedTerminalResultCandidate(
            candidate_id=mcp_terminal_candidate_id("call-1", digest),
            owner_user_id="owner-a",
            conversation_id="conv-1",
            task_id="task-1",
            node_id="node-1",
            intent_id="intent-1",
            call_id="call-1",
            server_id="server-1",
            server_config_version=1,
            server_security_version=1,
            terminal_state=MCPTerminalState.COMPLETED,
            result_payload_sha256=digest,
            safe_result_ref="artifact:safe-result",
            safe_result_ref_sha256=canonical_sha256({"ref": 1}),
            safe_error_code=None,
            sealed_at=self.at,
        )
        self.storage = SQLiteStorage(
            self.sessions,
            mcp_terminal_candidate_reader=lambda call_id, candidate_id: self.candidate,
        )
        with self.sessions() as session:
            session.add(
                ConversationRow(
                    conversation_id="conv-1",
                    username="owner-a",
                    status="active",
                    created_at=self.at,
                    updated_at=self.at,
                )
            )
            session.add(
                TaskRow(
                    task_id="task-1",
                    conversation_id="conv-1",
                    root_message_id="message-1",
                    status="running",
                    routing_mode="auto",
                    requested_capability_id=None,
                    root_node_id="node-1",
                    summary=None,
                    cancel_requested_at=None,
                    created_at=self.at,
                    updated_at=self.at,
                    mcp_execution_mode="user_scoped",
                    mcp_shadow_enabled=False,
                    mcp_rollout_config_version="cp7",
                    mcp_route_reason_code="enforce_selected",
                    mcp_rollout_mode="enforce",
                )
            )
            session.add(
                TaskNodeRow(
                    node_id="node-1",
                    task_id="task-1",
                    capability_id="mcp.dispatch",
                    assigned_instance_id=None,
                    status="running",
                    criticality="required",
                    dependency_type="hard",
                    retry_policy={},
                    timeout_policy={},
                    resource_class=None,
                    input_refs=[],
                    output_refs=[],
                    started_at=self.at,
                    finished_at=None,
                )
            )
            session.add(
                MCPNoServerIntentRow(
                    intent_id="intent-1",
                    owner_user_id="owner-a",
                    task_id="task-1",
                    node_id="node-1",
                    trigger="target_server_revalidation",
                    requested_server_id="server-1",
                    requested_server_config_version=1,
                    requested_server_security_version=1,
                    owner_server_set_fingerprint=None,
                    resume_envelope_json={"task_id": "task-1"},
                    resume_envelope_sha256=canonical_sha256({"task_id": "task-1"}),
                    status="dispatched",
                    revision=2,
                    evidence_sha256=canonical_sha256({"intent": 1}),
                    created_at=self.at,
                    updated_at=self.at,
                    terminal_at=None,
                )
            )
            session.add(
                MCPDispatchResumeOutboxRow(
                    outbox_id="outbox-1",
                    intent_id="intent-1",
                    owner_user_id="owner-a",
                    task_id="task-1",
                    node_id="node-1",
                    server_id="server-1",
                    resume_envelope_sha256=canonical_sha256({"task_id": "task-1"}),
                    payload_sha256=canonical_sha256({"outbox": 1}),
                    status="claimed",
                    claim_owner="worker-1",
                    claim_token="token-1",
                    lease_expires_at=datetime(2026, 8, 13, 5, 0),
                    revision=1,
                    created_at=self.at,
                    updated_at=self.at,
                    completed_at=None,
                    result_receipt_id=None,
                    completion_mode=None,
                )
            )
            session.add(
                MCPCallRecordRow(
                    call_ref="call-1",
                    branch_id="branch-1",
                    owner_user_id="owner-a",
                    task_id="task-1",
                    node_id="node-1",
                    server_id="server-1",
                    tool_name="safe_tool",
                    status="running",
                    call_sequence=1,
                    arguments_sha256=canonical_sha256({"args": 1}),
                    server_security_version=1,
                    server_config_version=1,
                    input_schema_sha256=canonical_sha256({"schema": 1}),
                    protocol_version="2026-07-28",
                    input_field_names=[],
                    may_have_dispatched=True,
                    result_ref=None,
                    output_size_bytes=None,
                    safe_error_code=None,
                    created_at=self.at,
                    updated_at=self.at,
                    terminal_at=None,
                )
            )
            session.commit()

    async def asyncTearDown(self) -> None:
        self.engine.dispose()
        self.temp_dir.cleanup()

    async def test_normal_commit_is_atomic_and_response_loss_retry_is_idempotent(self) -> None:
        result = await self.storage.commit_authoritative_mcp_terminal_result(
            "call-1", self.candidate.candidate_id, self.at
        )
        self.assertEqual(str(result), "committed_normal")
        retry = await self.storage.commit_authoritative_mcp_terminal_result(
            "call-1", self.candidate.candidate_id, self.at + timedelta(minutes=5)
        )
        self.assertEqual(str(retry), "already_committed")
        receipt = await self.storage.get_mcp_terminal_result_receipt(
            f"mcp-terminal-result:v1:call-1:{self.candidate.result_payload_sha256}"
        )
        self.assertEqual(receipt.candidate_id, self.candidate.candidate_id)
        self.assertEqual(receipt.committed_at, self.at)
        self.assertEqual(str((await self.storage.get_task("task-1")).status), "completed")
        with self.sessions() as session:
            terminal_events = session.scalars(
                select(EventRecordRow).where(
                    EventRecordRow.event_id
                    == (
                        f"mcp-terminal-result:v1:call-1:"
                        f"{self.candidate.result_payload_sha256}:terminal"
                    )
                )
            ).all()
            self.assertEqual(len(terminal_events), 1)

    async def test_no_server_convergence_defers_unknown_when_secure_candidate_exists(self) -> None:
        recovery_storage = SQLiteStorage(
            self.sessions,
            mcp_terminal_candidate_reader=lambda call_id, candidate_id: self.candidate,
            mcp_terminal_candidate_resolver=lambda call_id: self.candidate,
        )
        result = await recovery_storage.converge_user_mcp_no_server("task-1", self.at)
        self.assertEqual(str(result), "trusted_terminal_result_requires_commit")
        self.assertIsNone(
            await recovery_storage.get_mcp_execution_terminal_projection("call-1")
        )
        self.assertEqual(str((await recovery_storage.get_task("task-1")).status), "running")
        committed = await recovery_storage.commit_authoritative_mcp_terminal_result(
            "call-1", self.candidate.candidate_id, self.at
        )
        self.assertEqual(str(committed), "committed_normal")

    async def test_late_result_updates_only_projection_and_keeps_task_failed(self) -> None:
        unknown = await self.storage.converge_user_mcp_no_server("task-1", self.at)
        self.assertEqual(str(unknown), "unknown_requires_no_replay")
        result = await self.storage.commit_authoritative_mcp_terminal_result(
            "call-1", self.candidate.candidate_id, self.at
        )
        self.assertEqual(str(result), "committed_late")
        projection = await self.storage.get_mcp_execution_terminal_projection("call-1")
        self.assertEqual(str(projection.status), "late_result_resolved")
        self.assertTrue(projection.no_replay)
        self.assertEqual(str((await self.storage.get_task("task-1")).status), "failed")
        call = await self.storage.get_mcp_call_record("owner-a", "task-1", "call-1")
        self.assertEqual(call.status, "execution_status_unknown")
        with self.sessions() as session:
            events = session.scalars(
                select(EventRecordRow)
                .where(EventRecordRow.task_id == "task-1")
                .order_by(EventRecordRow.created_at, EventRecordRow.event_id)
            ).all()
            self.assertEqual(
                [event.event_type for event in events],
                [
                    "mcp.execution_status_unknown",
                    "task.failed",
                    "mcp.execution_status_resolution",
                    "mcp.late_terminal_result_recovered",
                ],
            )


if __name__ == "__main__":
    unittest.main()
