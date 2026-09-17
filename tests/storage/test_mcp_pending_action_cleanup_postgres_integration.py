from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import delete

from src.storage.postgres import (
    bootstrap_postgres_database,
    create_postgres_engine,
    create_postgres_session_factory,
)
from src.storage.postgres.repositories import PostgreSQLStorage
from src.storage.sqlalchemy_models import (
    MCPCallRecordRow,
    MCPPendingToolActionRow,
    MCPTerminalResultReceiptRow,
)
from tests.postgres_test_support import isolated_postgres_test_dsn_or_skip_reason


NOW = datetime(2026, 8, 29, 8, 0, 0)


class MCPPendingActionCleanupPostgresIntegrationTest(
    unittest.IsolatedAsyncioTestCase
):
    @classmethod
    def setUpClass(cls) -> None:
        dsn, skip_reason = isolated_postgres_test_dsn_or_skip_reason(
            "MAF_POSTGRES_MCP_CLEANUP_TEST_DSN",
            fallback_env="MAF_POSTGRES_TEST_DSN",
        )
        if skip_reason:
            raise unittest.SkipTest(skip_reason)
        assert dsn is not None
        cls.engine = create_postgres_engine(dsn, pool_size=2, max_overflow=0)
        bootstrap_postgres_database(cls.engine)
        cls.session_factory = create_postgres_session_factory(cls.engine)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.dispose()

    async def asyncSetUp(self) -> None:
        suffix = uuid4().hex
        self.action_id = f"mcp-cleanup-action-{suffix}"
        self.call_ref = f"mcp-cleanup-call-{suffix}"
        self.payload_ref = f"mcp-cleanup-payload-{suffix}"
        self.receipt_id = f"mcp-cleanup-receipt-{suffix}"
        self.candidate_id = f"mcp-cleanup-candidate-{suffix}"
        self.storage = PostgreSQLStorage(self.session_factory)
        with self.session_factory() as session:
            session.add(
                MCPPendingToolActionRow(
                    action_id=self.action_id,
                    owner_user_id="mcp-cleanup-owner",
                    conversation_id=f"mcp-cleanup-conversation-{suffix}",
                    task_id=f"mcp-cleanup-task-{suffix}",
                    node_id=f"mcp-cleanup-node-{suffix}",
                    server_id=f"mcp-cleanup-server-{suffix}",
                    tool_name="lookup",
                    arguments_sha256="sha256:arguments",
                    approval_fingerprint="sha256:approval",
                    arguments_payload_ref=self.payload_ref,
                    payload_file_sha256="sha256:file",
                    payload_size_bytes=123,
                    encryption_version=1,
                    server_config_version=1,
                    server_security_version=1,
                    input_schema_sha256="sha256:schema",
                    status="approved",
                    revision=1,
                    approved_at=NOW,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            session.commit()

    async def asyncTearDown(self) -> None:
        with self.session_factory() as session:
            session.execute(
                delete(MCPTerminalResultReceiptRow).where(
                    MCPTerminalResultReceiptRow.result_receipt_id == self.receipt_id
                )
            )
            session.execute(
                delete(MCPCallRecordRow).where(
                    MCPCallRecordRow.call_ref == self.call_ref
                )
            )
            session.execute(
                delete(MCPPendingToolActionRow).where(
                    MCPPendingToolActionRow.action_id == self.action_id
                )
            )
            session.commit()

    async def test_protection_matches_terminal_receipt_semantics(self) -> None:
        protected = (
            await self.storage.list_protected_mcp_pending_action_payload_refs()
        )
        self.assertIn(self.payload_ref, protected)

        with self.session_factory() as session:
            action = session.get(MCPPendingToolActionRow, self.action_id)
            action.status = "consumed"
            action.revision = 2
            action.consumed_at = NOW + timedelta(seconds=1)
            action.updated_at = NOW + timedelta(seconds=1)
            session.add(
                MCPCallRecordRow(
                    call_ref=self.call_ref,
                    branch_id=f"mcp-cleanup-branch-{self.action_id}",
                    owner_user_id=action.owner_user_id,
                    task_id=action.task_id,
                    node_id=action.node_id,
                    server_id=action.server_id,
                    tool_name=action.tool_name,
                    status="failed",
                    call_sequence=1,
                    arguments_sha256=action.arguments_sha256,
                    server_security_version=action.server_security_version,
                    server_config_version=action.server_config_version,
                    input_schema_sha256=action.input_schema_sha256,
                    may_have_dispatched=True,
                    safe_error_code="remote_failed",
                    pending_action_id=action.action_id,
                    created_at=NOW,
                    updated_at=NOW + timedelta(seconds=1),
                    terminal_at=NOW + timedelta(seconds=1),
                )
            )
            session.add(
                MCPTerminalResultReceiptRow(
                    result_receipt_id=self.receipt_id,
                    candidate_id=self.candidate_id,
                    owner_user_id=action.owner_user_id,
                    conversation_id=action.conversation_id,
                    task_id=action.task_id,
                    node_id=action.node_id,
                    intent_id=f"mcp-cleanup-intent-{self.action_id}",
                    call_id=self.call_ref,
                    server_id=action.server_id,
                    server_config_version=action.server_config_version,
                    server_security_version=action.server_security_version,
                    terminal_state="failed",
                    result_payload_sha256="sha256:" + "1" * 64,
                    safe_result_ref=None,
                    safe_result_ref_sha256=None,
                    safe_error_code="remote_failed",
                    safe_result_content_sha256=None,
                    safe_result_size_bytes=None,
                    safe_result_store_kind=None,
                    result_parser_revision=None,
                    validated_checkpoint_sha256=None,
                    parsed_model_sha256=None,
                    completion_mode="normal_terminal_projection",
                    committed_at=NOW + timedelta(seconds=1),
                )
            )
            session.commit()

        protected = (
            await self.storage.list_protected_mcp_pending_action_payload_refs()
        )
        self.assertNotIn(self.payload_ref, protected)


if __name__ == "__main__":
    unittest.main()
