from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from src.api.runtime import build_api_runtime
from src.core.enums import UserMCPHealthStatus, UserMCPTransport
from src.core.models import UserMCPServer
from src.orchestration.models import OrchestrationRequest
from src.integrations.mcp.credentials import CredentialSecurityError


class UserMCPRuntimeWiringTest(unittest.IsolatedAsyncioTestCase):
    def test_enabled_feature_fails_closed_without_key_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "MAF_STATE_STORE_BACKEND": "sqlite",
                "MAF_USER_MCP_MAX_ACTIVE_CALLS": "2",
                "MAF_USER_MCP_TEMPORARY_DISK_LOW_WATERMARK_BYTES": "1",
            },
            clear=False,
        ):
            with self.assertRaisesRegex(
                CredentialSecurityError, "mcp_credential_key_file_missing"
            ):
                build_api_runtime(
                    database_path=Path(directory) / "runtime.sqlite3",
                    audit_log_path=Path(directory) / "audit.jsonl",
                    enable_user_mcp=True,
                    enable_platform_llm=False,
                    enable_llm_planner=False,
                    enable_conversation_title_llm=False,
                    enable_conversation_memory=False,
                )

    async def test_enabled_feature_verifies_sentinel_before_ready(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "MAF_STATE_STORE_BACKEND": "sqlite",
                "MAF_USER_MCP_MAX_ACTIVE_CALLS": "2",
                "MAF_USER_MCP_TEMPORARY_DISK_LOW_WATERMARK_BYTES": "1",
            },
            clear=False,
        ):
            root = Path(directory)
            key_path = root / "mcp.key"
            key_path.write_text("YWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWE=", encoding="ascii")
            key_path.chmod(0o600)
            runtime = build_api_runtime(
                database_path=root / "runtime.sqlite3",
                audit_log_path=root / "audit.jsonl",
                enable_user_mcp=True,
                user_mcp_credential_key_file=key_path,
                enable_platform_llm=False,
                enable_llm_planner=False,
                enable_conversation_title_llm=False,
                enable_conversation_memory=False,
            )
            await runtime.start()
            try:
                self.assertIsNotNone(runtime.user_mcp_config_service)
                self.assertIsNotNone(
                    await runtime.storage.get_mcp_credential_key_validation()
                )
            finally:
                await runtime.shutdown()

    async def test_routing_flag_registers_only_dispatch_and_injects_safe_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "MAF_STATE_STORE_BACKEND": "sqlite",
                "MAF_USER_MCP_MAX_ACTIVE_CALLS": "2",
                "MAF_USER_MCP_TEMPORARY_DISK_LOW_WATERMARK_BYTES": "1",
            },
            clear=False,
        ):
            root = Path(directory)
            key_path = root / "mcp.key"
            key_path.write_text(
                "YWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWE=",
                encoding="ascii",
            )
            key_path.chmod(0o600)
            runtime = build_api_runtime(
                database_path=root / "runtime.sqlite3",
                audit_log_path=root / "audit.jsonl",
                enable_user_mcp=True,
                enable_user_mcp_routing=True,
                user_mcp_credential_key_file=key_path,
                planner_text_generator=lambda _prompt, **_kwargs: '{"action":"finish","reason":"done"}',
                enable_platform_llm=False,
                enable_conversation_title_llm=False,
                enable_conversation_memory=False,
            )
            now = datetime(2026, 8, 12, 12, 0, 0)
            await runtime.storage.create_user_mcp_server(
                UserMCPServer(
                    server_id="server-a",
                    owner_user_id="alice",
                    display_name="CRM",
                    routing_description="客户查询",
                    endpoint_url="https://secret.invalid/mcp",
                    transport=UserMCPTransport.STREAMABLE_HTTP,
                    health_status=UserMCPHealthStatus.AVAILABLE,
                    created_at=now,
                    updated_at=now,
                )
            )
            try:
                self.assertIsNotNone(runtime.capability_registry.get("mcp.dispatch"))
                profiles = await runtime.available_user_mcp_server_profiles("alice")
                self.assertEqual(len(profiles), 1)
                self.assertEqual(profiles[0].display_name, "CRM")
                self.assertNotIn("secret.invalid", repr(profiles[0]))
                visible = runtime.capability_registry.list_for_request(
                    OrchestrationRequest(
                        task_id="task-a",
                        conversation_id="conv-a",
                        root_message_id="msg-a",
                        user_message="查客户",
                        available_mcp_servers=profiles,
                    ),
                    public_only=True,
                )
                self.assertIn("mcp.dispatch", {item.capability_id for item in visible})
            finally:
                await runtime.shutdown()
