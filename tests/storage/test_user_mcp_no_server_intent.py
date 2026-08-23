from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from src.core.enums import (
    ConversationStatus,
    NodeStatus,
    RoutingMode,
    TaskStatus,
    UserMCPHealthStatus,
    UserMCPTransport,
)
from src.core.models import (
    Conversation, MCPCallRecord, MCPCP7ReadyEpochEvent,
    MCPCP7ReadyEpochEventKind, MCPCP7SafetyLedgerRecord, MCPCP7SafetyRecordKind,
    Task, TaskNode, UserMCPHealthAttempt, UserMCPServer,
)
from src.integrations.mcp.cp7_artifacts import canonical_sha256
from src.integrations.mcp.resume_envelope import (
    build_mcp_dispatch_resume_envelope_v2,
)
from src.storage.sqlite.bootstrap import bootstrap_sqlite_database
from src.storage.sqlite.models import EventRecordRow, UserMCPOwnerMutationGuardRow
from src.storage.sqlite.repositories import SQLiteStorage


class UserMCPNoServerIntentTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "state.db"
        self.engine = create_engine(f"sqlite+pysqlite:///{self.db_path}")
        bootstrap_sqlite_database(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.storage = SQLiteStorage(self.sessions)
        self.at = datetime(2026, 8, 13, 1, 2, 3)
        await self.storage.save_conversation(
            Conversation(
                conversation_id="conv-1",
                username="owner-a",
                status=ConversationStatus.ACTIVE,
                created_at=self.at,
                updated_at=self.at,
            )
        )

    async def asyncTearDown(self) -> None:
        self.engine.dispose()
        self.temp_dir.cleanup()

    async def test_initial_intent_converges_once_and_survives_reopen(self) -> None:
        task = Task(
            task_id="task-1",
            conversation_id="conv-1",
            root_message_id="message-1",
            status=TaskStatus.ACCEPTED,
            routing_mode=RoutingMode.FORCE_CAPABILITY,
            requested_capability_id="mcp.dispatch",
            created_at=self.at,
            updated_at=self.at,
        )
        created = await self.storage.create_user_mcp_initial_intent(task, self.at)
        self.assertEqual(str(created), "created_unavailable")
        converged = await self.storage.converge_user_mcp_no_server("task-1", self.at)
        self.assertEqual(str(converged), "converged")
        retried = await self.storage.converge_user_mcp_no_server("task-1", self.at)
        self.assertEqual(str(retried), "already_converged")
        self.assertEqual((await self.storage.get_task("task-1")).status, TaskStatus.FAILED)
        with self.sessions() as session:
            events = session.scalars(
                select(EventRecordRow).where(EventRecordRow.task_id == "task-1")
            ).all()
            self.assertEqual(len(events), 2)
        reopened = SQLiteStorage(self.sessions)
        intent = await reopened.get_mcp_no_server_intent(
            "mcp-no-server-intent:v1:task-1:initial"
        )
        self.assertEqual(str(intent.status), "converged")

    async def test_missing_target_is_bound_unavailable_without_outbox(self) -> None:
        await self.storage.save_task(
            Task(
                task_id="task-2",
                conversation_id="conv-1",
                root_message_id="message-2",
                status=TaskStatus.RUNNING,
                routing_mode=RoutingMode.AUTO,
                created_at=self.at,
                updated_at=self.at,
                mcp_execution_mode="user_scoped",
                mcp_shadow_enabled=False,
                mcp_rollout_config_version="cp7",
                mcp_route_reason_code="enforce_selected",
                mcp_rollout_mode="enforce",
            )
        )
        await self.storage.save_task_node(
            TaskNode(
                node_id="node-2",
                task_id="task-2",
                capability_id="mcp.dispatch",
                status=NodeStatus.RUNNING,
            )
        )
        result = await self.storage.arm_user_mcp_target_intent(
            "task-2", "node-2", "missing-server", {"task_id": "task-2"}, self.at
        )
        self.assertEqual(str(result), "unavailable")
        intent = await self.storage.get_mcp_no_server_intent(
            "mcp-no-server-intent:v1:task-2:node-2"
        )
        self.assertEqual(str(intent.status), "unavailable")
        self.assertIsNone(
            await self.storage.get_mcp_dispatch_resume_outbox(
                "mcp-dispatch-resume:v1:mcp-no-server-intent:v1:task-2:node-2"
            )
        )

    def _available_server(self, server_id: str = "server-1") -> UserMCPServer:
        return UserMCPServer(
            server_id=server_id,
            owner_user_id="owner-a",
            display_name=server_id,
            routing_description="route",
            endpoint_url="https://example.test/mcp",
            transport=UserMCPTransport.STREAMABLE_HTTP,
            health_status=UserMCPHealthStatus.AVAILABLE,
            created_at=self.at,
            updated_at=self.at,
        )

    async def test_v2_envelope_is_validated_before_intent_write(self) -> None:
        await self.storage.create_user_mcp_server(self._available_server())
        task = Task(
            task_id="task-v2",
            conversation_id="conv-1",
            root_message_id="message-v2",
            status=TaskStatus.RUNNING,
            routing_mode=RoutingMode.AUTO,
            created_at=self.at,
            updated_at=self.at,
            mcp_execution_mode="user_scoped",
            mcp_shadow_enabled=False,
            mcp_rollout_config_version="cp7",
            mcp_route_reason_code="enforce_selected",
            mcp_rollout_mode="enforce",
        )
        node = TaskNode(
            node_id="node-v2",
            task_id=task.task_id,
            capability_id="mcp.dispatch",
            status=NodeStatus.RUNNING,
        )
        await self.storage.save_task(task)
        await self.storage.save_task_node(node)
        envelope = build_mcp_dispatch_resume_envelope_v2(
            task=task,
            node=node,
            attachments=(),
            server_id="server-1",
        )

        invalid = dict(envelope)
        invalid["metadata"] = {"content_base64": "forbidden"}
        with self.assertRaisesRegex(
            ValueError, "mcp_dispatch_resume_envelope_invalid"
        ):
            await self.storage.arm_user_mcp_target_intent(
                task.task_id, node.node_id, "server-1", invalid, self.at
            )
        self.assertIsNone(
            await self.storage.get_mcp_no_server_intent(
                "mcp-no-server-intent:v1:task-v2:node-v2"
            )
        )

        result = await self.storage.arm_user_mcp_target_intent(
            task.task_id, node.node_id, "server-1", envelope, self.at
        )
        self.assertEqual(str(result), "armed")

    async def test_health_mutation_refreshes_guard_before_initial_intent(self) -> None:
        await self.storage.create_user_mcp_server(self._available_server())
        attempt = UserMCPHealthAttempt(
            "health-1",
            "owner-a",
            "server-1",
            1,
            1,
            "runner-1",
            self.at + timedelta(minutes=1),
            self.at,
            self.at,
        )
        self.assertTrue(await self.storage.claim_user_mcp_health_attempt(attempt))
        completed = await self.storage.complete_user_mcp_health_attempt(
            "health-1",
            "owner-a",
            "server-1",
            runner_instance_id="runner-1",
            config_version=1,
            security_version=1,
            health_status="available",
            error_code=None,
            completed_at=self.at + timedelta(seconds=1),
        )
        self.assertIsNotNone(completed)
        result = await self.storage.create_user_mcp_initial_intent(
            Task(
                task_id="task-health",
                conversation_id="conv-1",
                root_message_id="message-health",
                status=TaskStatus.ACCEPTED,
                routing_mode=RoutingMode.FORCE_CAPABILITY,
                requested_capability_id="mcp.dispatch",
                created_at=self.at,
                updated_at=self.at,
            ),
            self.at + timedelta(seconds=2),
        )
        self.assertEqual(str(result), "retry_route")
        with self.sessions() as session:
            guard = session.get(UserMCPOwnerMutationGuardRow, "owner-a")
            self.assertEqual(guard.revision, 3)

    async def test_delete_between_target_arm_and_resolve_is_unavailable(self) -> None:
        await self.storage.create_user_mcp_server(self._available_server())
        await self.storage.save_task(
            Task(
                task_id="task-delete",
                conversation_id="conv-1",
                root_message_id="message-delete",
                status=TaskStatus.RUNNING,
                routing_mode=RoutingMode.AUTO,
                created_at=self.at,
                updated_at=self.at,
                mcp_execution_mode="user_scoped",
                mcp_shadow_enabled=False,
                mcp_rollout_config_version="cp7",
                mcp_route_reason_code="enforce_selected",
                mcp_rollout_mode="enforce",
            )
        )
        await self.storage.save_task_node(
            TaskNode(
                node_id="node-delete",
                task_id="task-delete",
                capability_id="mcp.dispatch",
                status=NodeStatus.RUNNING,
            )
        )
        armed = await self.storage.arm_user_mcp_target_intent(
            "task-delete",
            "node-delete",
            "server-1",
            {"task_id": "task-delete"},
            self.at,
        )
        self.assertEqual(str(armed), "armed")
        await self.storage.mark_user_mcp_server_deleted(
            "owner-a", "server-1", deleted_at=self.at + timedelta(seconds=1)
        )
        resolved = await self.storage.resolve_user_mcp_target_intent(
            "mcp-no-server-intent:v1:task-delete:node-delete",
            self.at + timedelta(seconds=2),
        )
        self.assertEqual(str(resolved), "unavailable")
        self.assertIsNone(
            await self.storage.get_mcp_dispatch_resume_outbox(
                "mcp-dispatch-resume:v1:mcp-no-server-intent:v1:task-delete:node-delete"
            )
        )

    async def test_expired_dispatch_resume_claim_is_reclaimed_without_call_replay(self) -> None:
        await self.storage.create_user_mcp_server(self._available_server())
        await self.storage.save_task(
            Task(
                task_id="task-claim-crash",
                conversation_id="conv-1",
                root_message_id="message-claim-crash",
                status=TaskStatus.RUNNING,
                routing_mode=RoutingMode.AUTO,
                created_at=self.at,
                updated_at=self.at,
                mcp_execution_mode="user_scoped",
                mcp_shadow_enabled=False,
                mcp_rollout_config_version="cp7",
                mcp_route_reason_code="enforce_selected",
                mcp_rollout_mode="enforce",
            )
        )
        await self.storage.save_task_node(
            TaskNode(
                node_id="node-claim-crash",
                task_id="task-claim-crash",
                capability_id="mcp.dispatch",
                status=NodeStatus.RUNNING,
            )
        )
        intent_id = "mcp-no-server-intent:v1:task-claim-crash:node-claim-crash"
        outbox_id = f"mcp-dispatch-resume:v1:{intent_id}"
        await self.storage.arm_user_mcp_target_intent(
            "task-claim-crash",
            "node-claim-crash",
            "server-1",
            {"task_id": "task-claim-crash"},
            self.at,
        )
        await self.storage.resolve_user_mcp_target_intent(
            intent_id, self.at + timedelta(seconds=1)
        )
        claimed = await self.storage.claim_mcp_dispatch_resume_outbox(
            outbox_id,
            "crashed-worker",
            "crashed-token",
            self.at + timedelta(seconds=2),
            self.at + timedelta(seconds=3),
        )
        self.assertIsNotNone(claimed)
        self.assertIsNone(
            await self.storage.reclaim_mcp_dispatch_resume_outbox(
                outbox_id, claimed.revision, self.at + timedelta(seconds=2)
            )
        )
        pending = await self.storage.reclaim_mcp_dispatch_resume_outbox(
            outbox_id, claimed.revision, self.at + timedelta(seconds=4)
        )
        self.assertEqual(str(pending.status), "pending")
        restarted = await self.storage.claim_mcp_dispatch_resume_outbox(
            outbox_id,
            "restart-worker",
            "restart-token",
            self.at + timedelta(seconds=4),
            self.at + timedelta(seconds=34),
        )
        self.assertEqual(str(restarted.status), "claimed")
        self.assertEqual(
            await self.storage.list_mcp_call_records("owner-a", "task-claim-crash"),
            [],
        )

    async def test_cp7_guard_latch_between_precheck_and_admit_writes_no_call(self) -> None:
        await self.storage.create_user_mcp_server(self._available_server())
        await self.storage.save_task(Task(
            task_id="task-race", conversation_id="conv-1", root_message_id="message-race",
            status=TaskStatus.RUNNING, routing_mode=RoutingMode.AUTO,
            created_at=self.at, updated_at=self.at, mcp_execution_mode="user_scoped",
            mcp_shadow_enabled=False, mcp_rollout_config_version="cp7",
            mcp_route_reason_code="enforce_selected", mcp_rollout_mode="enforce",
        ))
        await self.storage.save_task_node(TaskNode(
            node_id="node-race", task_id="task-race", capability_id="mcp.dispatch",
            status=NodeStatus.RUNNING,
        ))
        intent_id = "mcp-no-server-intent:v1:task-race:node-race"
        outbox_id = f"mcp-dispatch-resume:v1:{intent_id}"
        await self.storage.arm_user_mcp_target_intent(
            "task-race", "node-race", "server-1", {"task_id": "task-race"}, self.at
        )
        await self.storage.resolve_user_mcp_target_intent(intent_id, self.at)
        await self.storage.claim_mcp_dispatch_resume_outbox(
            outbox_id, "worker", "token", self.at, self.at + timedelta(minutes=1)
        )
        await self.storage.append_mcp_cp7_safety_ledger_record(MCPCP7SafetyLedgerRecord(
            record_id="fatal-gap", candidate_id="candidate-race", epoch_id="epoch-race",
            config_fingerprint="config", record_kind=MCPCP7SafetyRecordKind.GAP,
            red_line=None, hook_id=None, bucket_started_at=None, bucket_ended_at=None,
            reason_code="producer_interval_missed", value=1,
            boundary_source_sha256=canonical_sha256({"gap": 1}),
            payload_sha256=canonical_sha256({"record": 1}), recorded_at=self.at,
        ))
        await self.storage.append_mcp_cp7_ready_epoch_event(MCPCP7ReadyEpochEvent(
            event_id="ready-race", candidate_id="candidate-race", epoch_id="epoch-race",
            predecessor_epoch_id=None, event_kind=MCPCP7ReadyEpochEventKind.READY,
            container_id="container", image_id="image", config_fingerprint="config",
            boundary_at=self.at, audit_device="device", audit_inode=1, audit_offset=1,
            ledger_record_count=1, inflight_state_sha256=canonical_sha256({"inflight": 1}),
            payload_sha256=canonical_sha256({"ready": 1}),
        ))
        intent = await self.storage.get_mcp_no_server_intent(intent_id)
        outbox = await self.storage.get_mcp_dispatch_resume_outbox(outbox_id)
        admitted = await self.storage.admit_mcp_tool_call(
            intent_id, outbox_id, intent.revision, outbox.revision,
            MCPCallRecord(
                call_ref="call-race", branch_id="branch-race", owner_user_id="owner-a",
                task_id="task-race", node_id="node-race", server_id="server-1",
                tool_name="tool", status="reserved", call_sequence=1,
                arguments_sha256=canonical_sha256({"args": 1}), server_security_version=1,
                server_config_version=1, input_schema_sha256=canonical_sha256({"schema": 1}),
                protocol_version="2026-07-28", input_field_names=(), created_at=self.at,
                updated_at=self.at, may_have_dispatched=True,
            ), self.at, cp7_candidate_id="candidate-race", cp7_epoch_id="epoch-race",
        )
        self.assertFalse(admitted)
        self.assertIsNone(await self.storage.get_mcp_call_record(
            "owner-a", "task-race", "call-race"
        ))


if __name__ == "__main__":
    unittest.main()
