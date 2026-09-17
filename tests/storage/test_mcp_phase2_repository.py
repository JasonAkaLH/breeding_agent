from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from src.core.enums import UserMCPHealthStatus, UserMCPTransport
from src.core.models import (
    Conversation,
    MCPAuditEvent,
    MCPBranchRecord,
    MCPCallRecord,
    MCPConnectionLease,
    MCPRemoteTaskBinding,
    MCPSealedState,
    Task,
    UserMCPServer,
    UserMCPToolGrant,
)
from src.storage.sqlite import SQLiteStorage
from tests.storage.support import SQLiteStorageTestCase


class MCPPhaseTwoRepositoryTest(SQLiteStorageTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.storage = SQLiteStorage(self.session_factory)
        self.now = datetime(2026, 8, 12, 12, 0, 0)
        asyncio.run(self.storage.create_user_mcp_server(self._server("alice", "server-a")))
        asyncio.run(self.storage.create_user_mcp_server(self._server("bob", "server-b")))

    def _server(self, owner_user_id: str, server_id: str) -> UserMCPServer:
        return UserMCPServer(
            server_id=server_id,
            owner_user_id=owner_user_id,
            display_name=server_id,
            routing_description="route",
            endpoint_url="https://example.test/mcp",
            transport=UserMCPTransport.STREAMABLE_HTTP,
            health_status=UserMCPHealthStatus.AVAILABLE,
            created_at=self.now,
            updated_at=self.now,
        )

    def test_grants_are_owner_scoped_exact_and_invalidated_on_security_change(self) -> None:
        alice = UserMCPToolGrant(
            "grant-a", "alice", "server-a", "lookup", 1, "schema-a", self.now
        )
        bob = UserMCPToolGrant(
            "grant-b", "bob", "server-b", "lookup", 1, "schema-b", self.now
        )
        asyncio.run(self.storage.save_user_mcp_tool_grant(alice))
        asyncio.run(self.storage.save_user_mcp_tool_grant(bob))

        self.assertEqual(
            [grant.grant_id for grant in asyncio.run(self.storage.list_user_mcp_tool_grants("alice"))],
            ["grant-a"],
        )
        self.assertIsNone(
            asyncio.run(
                self.storage.get_valid_user_mcp_tool_grant(
                    "bob",
                    "server-a",
                    "lookup",
                    server_security_version=1,
                    input_schema_sha256="schema-a",
                )
            )
        )
        self.assertIsNotNone(
            asyncio.run(
                self.storage.get_valid_user_mcp_tool_grant(
                    "alice",
                    "server-a",
                    "lookup",
                    server_security_version=1,
                    input_schema_sha256="schema-a",
                )
            )
        )

        changed_at = self.now + timedelta(seconds=1)
        asyncio.run(
            self.storage.update_user_mcp_server(
                "alice",
                "server-a",
                changes={"endpoint_url": "https://changed.example/mcp"},
                updated_at=changed_at,
            )
        )
        stale = asyncio.run(self.storage.list_user_mcp_tool_grants("alice", "server-a"))[0]
        self.assertEqual((stale.invalidated_at, stale.invalid_reason), (changed_at, "security_changed"))
        self.assertIsNone(
            asyncio.run(
                self.storage.get_valid_user_mcp_tool_grant(
                    "alice",
                    "server-a",
                    "lookup",
                    server_security_version=1,
                    input_schema_sha256="schema-a",
                )
            )
        )
        self.assertFalse(asyncio.run(self.storage.delete_user_mcp_tool_grant_by_id("bob", "grant-a")))
        self.assertTrue(asyncio.run(self.storage.delete_user_mcp_tool_grant_by_id("alice", "grant-a")))
        self.assertEqual(asyncio.run(self.storage.clear_user_mcp_tool_grants("bob", "server-b")), 1)

    def test_branch_reservation_enforces_one_active_call_and_budget(self) -> None:
        branch = MCPBranchRecord(
            "branch-a", "alice", "task-a", "node-a", "ready", max_tool_calls=2,
            created_at=self.now, updated_at=self.now,
        )
        asyncio.run(self.storage.save_mcp_branch_record(branch))
        first = self._call("call-1", 1)
        second = self._call("call-2", 2)
        third = self._call("call-3", 3)

        self.assertTrue(asyncio.run(self.storage.reserve_mcp_call(first)))
        self.assertFalse(asyncio.run(self.storage.reserve_mcp_call(second)))
        self.assertIsNone(asyncio.run(self.storage.get_mcp_call_record("bob", "task-a", "call-1")))
        self.assertTrue(
            asyncio.run(
                self.storage.mark_mcp_call_may_have_dispatched(
                    "alice", "task-a", "call-1", updated_at=self.now + timedelta(seconds=1)
                )
            )
        )
        finished = asyncio.run(
            self.storage.finish_mcp_call(
                "alice",
                "task-a",
                "call-1",
                status="completed",
                terminal_at=self.now + timedelta(seconds=2),
                result_ref="result-a",
                output_size_bytes=12,
            )
        )
        self.assertTrue(finished.may_have_dispatched)
        self.assertTrue(asyncio.run(self.storage.reserve_mcp_call(second)))
        asyncio.run(
            self.storage.finish_mcp_call(
                "alice", "task-a", "call-2", status="failed",
                terminal_at=self.now + timedelta(seconds=3), safe_error_code="remote_error",
            )
        )
        self.assertFalse(asyncio.run(self.storage.reserve_mcp_call(third)))
        saved = asyncio.run(self.storage.get_mcp_branch_record("alice", "task-a", "branch-a"))
        self.assertEqual((saved.tool_call_count, saved.active_call_ref), (2, None))

    def _call(self, call_ref: str, sequence: int) -> MCPCallRecord:
        return MCPCallRecord(
            call_ref=call_ref,
            branch_id="branch-a",
            owner_user_id="alice",
            task_id="task-a",
            node_id="node-a",
            server_id="server-a",
            tool_name="lookup",
            status="reserved",
            call_sequence=sequence,
            arguments_sha256=f"args-{sequence}",
            server_security_version=1,
            input_schema_sha256="schema-a",
            input_field_names=("query",),
            created_at=self.now,
            updated_at=self.now,
        )

    def test_remote_task_binding_preserves_legacy_positional_field_order(self) -> None:
        timestamps = tuple(self.now + timedelta(seconds=offset) for offset in range(4))
        binding = MCPRemoteTaskBinding(
            "remote-safe", "alice", "task-a", "node-a", "call-a", "server-a",
            "2026-07-28", b"cipher-remote", b"nonce-remote", 1, "working",
            *timestamps,
            published_at=self.now,
            continuation_plan={"plan_id": "plan-a"},
        )

        self.assertEqual(
            (binding.next_poll_at, binding.created_at, binding.updated_at, binding.terminal_at),
            timestamps,
        )
        self.assertEqual(binding.published_at, self.now)
        self.assertEqual(binding.continuation_plan, {"plan_id": "plan-a"})

    def test_recovery_records_leases_and_audit_are_owner_scoped(self) -> None:
        remote = MCPRemoteTaskBinding(
            "remote-safe", "alice", "task-a", "node-a", "call-a", "server-a",
            "2026-07-28", b"cipher-remote", b"nonce-remote", 1, "working",
            self.now, self.now, self.now,
        )
        sealed = MCPSealedState(
            "sealed-a", "alice", "task-a", "node-a", "call-a", "mrtr_request_state",
            b"cipher-state", b"nonce-state", 1, self.now, self.now,
        )
        asyncio.run(self.storage.save_mcp_remote_task_binding(remote))
        asyncio.run(self.storage.save_mcp_sealed_state(sealed))
        self.assertIsNone(
            asyncio.run(self.storage.get_mcp_remote_task_binding("bob", "task-a", "remote-safe"))
        )
        self.assertIsNone(asyncio.run(self.storage.get_mcp_sealed_state("bob", "task-a", "sealed-a")))
        self.assertEqual(
            asyncio.run(self.storage.get_mcp_remote_task_binding("alice", "task-a", "remote-safe")),
            remote,
        )
        self.assertEqual(asyncio.run(self.storage.get_mcp_sealed_state("alice", "task-a", "sealed-a")), sealed)

        live = MCPConnectionLease(
            "connection-live", "alice", "task-a", "api-a", self.now + timedelta(minutes=1),
            created_at=self.now, updated_at=self.now,
        )
        expired = MCPConnectionLease(
            "connection-expired", "alice", "task-a", "api-a", self.now - timedelta(seconds=1),
            created_at=self.now, updated_at=self.now,
        )
        asyncio.run(self.storage.save_mcp_connection_lease(live))
        asyncio.run(self.storage.save_mcp_connection_lease(expired))
        self.assertEqual(
            [item.connection_id for item in asyncio.run(
                self.storage.list_live_mcp_connection_leases("alice", "task-a", now=self.now)
            )],
            ["connection-live"],
        )
        self.assertEqual(asyncio.run(self.storage.expire_mcp_connection_leases(now=self.now)), 1)

        expired_audit = MCPAuditEvent(
            "audit-expired", "alice", "mcp.tool_call_started", self.now - timedelta(days=31),
            self.now - timedelta(days=1), task_id="task-a", safe_payload={"tool_name": "lookup"},
        )
        live_audit = MCPAuditEvent(
            "audit-live", "alice", "mcp.tool_call_completed", self.now,
            self.now + timedelta(days=30), task_id="task-a", safe_payload={"status": "completed"},
        )
        asyncio.run(self.storage.append_mcp_audit_event(expired_audit))
        asyncio.run(self.storage.append_mcp_audit_event(live_audit))
        self.assertEqual(asyncio.run(self.storage.list_mcp_audit_events("bob")), [])
        self.assertEqual(asyncio.run(self.storage.delete_expired_mcp_audit_events(now=self.now)), 1)
        self.assertEqual(
            [item.audit_event_id for item in asyncio.run(self.storage.list_mcp_audit_events("alice"))],
            ["audit-live"],
        )

    def test_conversation_delete_removes_task_scoped_mcp_records(self) -> None:
        asyncio.run(self.storage.save_conversation(Conversation("conversation-delete", "alice")))
        asyncio.run(
            self.storage.save_task(Task("task-delete", "conversation-delete", "message-root"))
        )
        asyncio.run(
            self.storage.save_mcp_branch_record(
                MCPBranchRecord(
                    "branch-delete", "alice", "task-delete", "node-delete", "ready",
                    created_at=self.now, updated_at=self.now,
                )
            )
        )
        call = MCPCallRecord(
            "call-delete", "branch-delete", "alice", "task-delete", "node-delete",
            "server-a", "lookup", "reserved", 1, "args", 1, "schema",
            created_at=self.now, updated_at=self.now,
        )
        self.assertTrue(asyncio.run(self.storage.reserve_mcp_call(call)))
        asyncio.run(
            self.storage.save_mcp_remote_task_binding(
                MCPRemoteTaskBinding(
                    "remote-delete", "alice", "task-delete", "node-delete", "call-delete",
                    "server-a", "2026-07-28", b"cipher", b"nonce", 1, "working",
                    self.now, self.now, self.now,
                )
            )
        )
        asyncio.run(
            self.storage.save_mcp_sealed_state(
                MCPSealedState(
                    "sealed-delete", "alice", "task-delete", "node-delete", "call-delete",
                    "mrtr", b"cipher", b"nonce", 1, self.now, self.now,
                )
            )
        )
        asyncio.run(
            self.storage.save_mcp_connection_lease(
                MCPConnectionLease(
                    "connection-delete", "alice", "task-delete", "api-a",
                    self.now + timedelta(minutes=1), created_at=self.now, updated_at=self.now,
                )
            )
        )
        asyncio.run(
            self.storage.append_mcp_audit_event(
                MCPAuditEvent(
                    "audit-delete", "alice", "mcp.tool_call_started", self.now,
                    self.now + timedelta(days=30), task_id="task-delete",
                )
            )
        )

        deleted = asyncio.run(self.storage.delete_conversation("conversation-delete"))

        for table_name in (
            "mcp_remote_task_binding", "mcp_sealed_state", "mcp_call_record",
            "mcp_branch_record", "mcp_connection_lease", "mcp_audit_event",
        ):
            self.assertEqual(deleted[table_name], 1)
        self.assertIsNone(
            asyncio.run(self.storage.get_mcp_branch_record("alice", "task-delete", "branch-delete"))
        )
