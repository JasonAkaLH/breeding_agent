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
from src.core.models import Conversation, Task, TaskNode, UserMCPHealthAttempt, UserMCPServer
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


if __name__ == "__main__":
    unittest.main()
