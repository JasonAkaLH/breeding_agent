from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.api.runtime import build_api_runtime
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
