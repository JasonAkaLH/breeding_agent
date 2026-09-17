from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from src.core.models import MCPBranchRecord, MCPCallRecord
from src.integrations.mcp.rollout import MCPExecutionPath, MCPRolloutConfig
from src.storage.sqlite import (
    SQLiteStorage,
    bootstrap_sqlite_database,
    create_sqlite_engine,
    create_sqlite_session_factory,
)


class UserMCPRolloutRollbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.engine = create_sqlite_engine(
            Path(self._tmpdir.name) / "rollout-rollback.sqlite3"
        )
        bootstrap_sqlite_database(self.engine)
        self.storage = SQLiteStorage(create_sqlite_session_factory(self.engine))

    async def asyncTearDown(self) -> None:
        self.engine.dispose()
        self._tmpdir.cleanup()

    async def test_flag_rollback_changes_only_new_task_assignments(self) -> None:
        enforce = MCPRolloutConfig.from_env(
            {
                "MCP_USER_SCOPED_GATEWAY_ENABLED": "true",
                "MCP_ROUTING_MODE": "enforce",
                "MCP_LEGACY_GLOBAL_RUNTIME_ENABLED": "true",
                "MCP_ENFORCE_PERCENT": "100",
                "MCP_ENFORCE_HASH_SALT": "phase3-fixed-salt",
            }
        )
        in_flight = enforce.assign_authenticated_user("alice")
        rollback = MCPRolloutConfig.from_env({})
        after_rollback = rollback.assign_authenticated_user("alice")

        self.assertEqual(in_flight.real_path, MCPExecutionPath.USER_SCOPED)
        self.assertEqual(after_rollback.real_path, MCPExecutionPath.LEGACY)
        self.assertEqual(in_flight.real_path, MCPExecutionPath.USER_SCOPED)
        self.assertNotEqual(in_flight.config_version, after_rollback.config_version)

    async def test_full_rollback_makes_custom_user_server_explicitly_unavailable(self) -> None:
        rollback = MCPRolloutConfig.from_env(
            {
                "MCP_USER_SCOPED_GATEWAY_ENABLED": "false",
                "MCP_ROUTING_MODE": "off",
                "MCP_LEGACY_GLOBAL_RUNTIME_ENABLED": "true",
            }
        )

        assignment = rollback.assign_authenticated_user(
            "custom-server-owner",
            has_user_scoped_server=True,
        )

        self.assertEqual(assignment.real_path, MCPExecutionPath.UNAVAILABLE)
        self.assertEqual(
            assignment.reason_code.value,
            "user_server_rollout_unavailable",
        )
        self.assertFalse(assignment.shadow_enabled)

    async def test_full_rollback_keeps_system_legacy_for_owner_without_custom_server(self) -> None:
        assignment = MCPRolloutConfig.from_env({}).assign_authenticated_user(
            "legacy-only-owner"
        )

        self.assertEqual(assignment.real_path, MCPExecutionPath.LEGACY)
        self.assertEqual(assignment.reason_code.value, "routing_off")

    async def test_restart_converges_ordinary_dispatched_call_to_unknown_without_replay(self) -> None:
        now = datetime(2026, 8, 13, 12, 0, 0)
        await self.storage.save_mcp_branch_record(
            MCPBranchRecord(
                branch_id="branch",
                owner_user_id="alice",
                task_id="task",
                node_id="node",
                status="active",
                created_at=now,
                updated_at=now,
            )
        )
        self.assertTrue(
            await self.storage.reserve_mcp_call(
                MCPCallRecord(
                    call_ref="call",
                    branch_id="branch",
                    owner_user_id="alice",
                    task_id="task",
                    node_id="node",
                    server_id="server",
                    tool_name="ordinary-tool",
                    status="active",
                    call_sequence=1,
                    arguments_sha256="arguments",
                    server_security_version=1,
                    input_schema_sha256="schema",
                    protocol_version="2025-11-25",
                    may_have_dispatched=True,
                    created_at=now,
                    updated_at=now,
                )
            )
        )
        converged = await self.storage.converge_dispatched_mcp_calls_to_unknown(
            now=now
        )
        converged_again = await self.storage.converge_dispatched_mcp_calls_to_unknown(
            now=now
        )

        self.assertEqual([call.call_ref for call in converged], ["call"])
        self.assertEqual(converged_again, [])
        stored = await self.storage.get_mcp_call_record("alice", "task", "call")
        self.assertIsNotNone(stored)
        self.assertEqual(stored.status, "unknown")
        self.assertEqual(stored.safe_error_code, "execution_status_unknown")
        self.assertEqual(stored.terminal_at, now)


if __name__ == "__main__":
    unittest.main()
