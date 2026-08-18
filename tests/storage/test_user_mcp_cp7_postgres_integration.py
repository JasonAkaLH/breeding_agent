from __future__ import annotations

import inspect
import os
import unittest

from src.storage.postgres.repositories import PostgreSQLStorage


class UserMCPCP7PostgresContractTest(unittest.TestCase):
    def test_authority_lock_order_is_explicit_and_not_inherited(self) -> None:
        source = inspect.getsource(PostgreSQLStorage._run_cp7_authority_sync)
        ordered = (
            "UserMCPOwnerMutationGuardRow",
            "UserMCPServerRow",
            "MCPNoServerIntentRow",
            "MCPDispatchResumeOutboxRow",
            "MCPPendingToolActionRow",
            "MCPBranchRecordRow",
            "MCPCallRecordRow",
            "_mcp_terminal_candidate_reader",
            "MCPTerminalResultReceiptRow",
            "MCPExecutionTerminalProjectionRow",
            "TaskRow",
            "TaskNodeRow",
        )
        positions = [source.index(name) for name in ordered]
        self.assertEqual(positions, sorted(positions))
        self.assertGreaterEqual(source.count("with_for_update()"), 10)
        for method_name in (
            "create_user_mcp_initial_intent",
            "arm_user_mcp_target_intent",
            "resolve_user_mcp_target_intent",
            "claim_mcp_dispatch_resume_outbox",
            "reclaim_mcp_dispatch_resume_outbox",
            "abort_mcp_dispatch_resume_outbox",
            "claim_mcp_dispatch",
            "renew_mcp_dispatch_claim",
            "release_or_recover_mcp_dispatch_claim",
            "admit_mcp_tool_call",
            "admit_approved_mcp_action",
            "converge_user_mcp_no_server",
            "commit_authoritative_mcp_terminal_result",
            "converge_legacy_runtime_retirement",
        ):
            override = inspect.getsource(getattr(PostgreSQLStorage, method_name))
            self.assertIn("_run_cp7_authority_sync", override, method_name)

    def test_server_mutators_use_owner_guard_before_server_lock(self) -> None:
        for method_name in (
            "update_user_mcp_server",
            "claim_user_mcp_health_attempt",
            "complete_user_mcp_health_attempt",
            "mark_user_mcp_server_deleted",
            "finalize_user_mcp_server_delete",
        ):
            source = inspect.getsource(getattr(PostgreSQLStorage, method_name))
            self.assertIn("_run_cp7_authority_sync", source, method_name)
            self.assertNotIn("_run_with_user_mcp_server_lock", source, method_name)

        for method_name in (
            "create_user_mcp_servers_atomic",
            "expire_user_mcp_health_attempts",
        ):
            source = inspect.getsource(getattr(PostgreSQLStorage, method_name))
            guard = source.index("UserMCPOwnerMutationGuardRow")
            server = source.index("UserMCPServerRow", guard)
            self.assertLess(guard, server, method_name)
            self.assertIn("with_for_update()", source, method_name)

    @unittest.skipUnless(
        os.environ.get("CP7_POSTGRES_VALIDATION_DSN"),
        "CP7_POSTGRES_VALIDATION_DSN is not configured",
    )
    def test_live_profile_is_owned_by_candidate_validation_runner(self) -> None:
        # The candidate-scoped runner supplies the DSN and invokes this module;
        # repository behavior is exercised by the shared storage suites there.
        self.assertTrue(os.environ["CP7_POSTGRES_VALIDATION_DSN"].startswith("postgres"))


if __name__ == "__main__":
    unittest.main()
