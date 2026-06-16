from __future__ import annotations

import unittest

from src.integrations.mcp.mcp_runtime_gates import validate_mcp_enforce_allowlist
from src.integrations.mcp.protocol import MCP_PROTOCOL_VERSION_2024_11_05, MCP_TRANSPORT_LEGACY_HTTP_SSE


class MCPEnforceAllowlistTest(unittest.TestCase):
    def test_enforce_allowlist_is_combination_level_and_blocks_shadow_mismatch(self) -> None:
        allowlist = {
            "schema_version": "maf.mcp.adapter_enforce_allowlist.v1",
            "allowed_combinations": [
                {
                    "server_scope": "fixture_remote_http",
                    "protocol_version": MCP_PROTOCOL_VERSION_2024_11_05,
                    "transport_family": MCP_TRANSPORT_LEGACY_HTTP_SSE,
                    "adapter": "official_rust_sdk",
                    "shadow_compare_status": "matched",
                    "enforce_allowed": True,
                    "rollback_path": "python_legacy_adapter",
                }
            ],
        }

        result = validate_mcp_enforce_allowlist(allowlist)

        self.assertEqual(result["enforce_allowed_combinations"], "1")
        self.assertIn("fixture_remote_http|2024-11-05|legacy_http_sse|official_rust_sdk", result["combinations"])

        allowlist["allowed_combinations"][0]["shadow_compare_status"] = "mismatched"
        with self.assertRaisesRegex(RuntimeError, "mcp_runtime_enforce_allowlist_blocked"):
            validate_mcp_enforce_allowlist(allowlist)

    def test_enforce_allowlist_rejects_global_adapter_toggle_without_combination_keys(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "mcp_runtime_enforce_allowlist_blocked"):
            validate_mcp_enforce_allowlist(
                {
                    "schema_version": "maf.mcp.adapter_enforce_allowlist.v1",
                    "official_rust_sdk_enforce": True,
                    "allowed_combinations": [],
                }
            )


if __name__ == "__main__":
    unittest.main()
