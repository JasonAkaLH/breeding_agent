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
    MCPBranchRecord,
    MCPCallRecord,
    MCPPendingActionPayloadSnapshot,
    Task,
    TaskNode,
    UserMCPServer,
    UserMCPToolGrant,
)
from src.integrations.mcp.cp7_artifacts import mcp_no_server_intent_id
from src.storage.sqlite import (
    SQLiteStorage,
    bootstrap_sqlite_database,
    create_sqlite_engine,
    create_sqlite_session_factory,
)
from src.storage.sqlite.models import MCPPendingToolActionRow


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
        self.storage = SQLiteStorage(
            self.sessions,
            mcp_pending_action_payload_reader=self.reader,
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

    def _call(self, call_ref: str = "call-1") -> MCPCallRecord:
        return MCPCallRecord(
            call_ref=call_ref,
            branch_id="branch-1",
            owner_user_id="alice",
            task_id=self.task.task_id,
            node_id=self.node.node_id,
            server_id=self.server.server_id,
            tool_name="lookup",
            status="reserved",
            call_sequence=1,
            arguments_sha256="sha256:arguments",
            server_security_version=self.server.security_version,
            server_config_version=self.server.config_version,
            input_schema_sha256="sha256:schema",
            protocol_version="2026-07-28",
            pending_action_id="action-1",
            created_at=NOW,
            updated_at=NOW,
        )

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
