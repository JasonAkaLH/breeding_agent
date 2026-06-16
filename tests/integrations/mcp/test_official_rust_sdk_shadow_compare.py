from __future__ import annotations

import tomllib
import unittest
from pathlib import Path

from src.integrations.mcp.mcp_runtime_gates import validate_mcp_official_rust_sdk_shadow_compare
from src.integrations.mcp.protocol import (
    MCP_PROTOCOL_VERSION_2024_11_05,
    MCP_PROTOCOL_VERSION_2025_03_26,
    MCP_PROTOCOL_VERSION_2025_06_18,
    MCP_PROTOCOL_VERSION_2025_11_25,
    MCP_TRANSPORT_LEGACY_HTTP_SSE,
    MCP_TRANSPORT_STREAMABLE_HTTP,
)


class OfficialRustSDKShadowCompareTest(unittest.TestCase):
    def test_rmcp_dependency_is_pinned_client_only_and_lockfile_tracked(self) -> None:
        workspace = tomllib.loads(Path("native/Cargo.toml").read_text(encoding="utf-8"))
        rmcp = workspace["workspace"]["dependencies"]["rmcp"]

        self.assertEqual(rmcp["version"], "1.7.0")
        self.assertFalse(rmcp["default-features"])
        self.assertEqual(
            rmcp["features"],
            ["client", "transport-streamable-http-client-reqwest", "reqwest"],
        )
        self.assertNotIn("server", rmcp["features"])
        self.assertNotIn("macros", rmcp["features"])

        crate = tomllib.loads(Path("native/crates/maf_mcp_runtime/Cargo.toml").read_text(encoding="utf-8"))
        self.assertTrue(crate["dependencies"]["rmcp"]["workspace"])
        lock = Path("native/Cargo.lock").read_text(encoding="utf-8")
        rmcp_start = lock.index('name = "rmcp"')
        next_package = lock.find("[[package]]", rmcp_start + 1)
        rmcp_block = lock[rmcp_start:] if next_package == -1 else lock[rmcp_start:next_package]
        self.assertIn('version = "1.7.0"', rmcp_block)
        self.assertNotIn('name = "rmcp-macros"', lock)

    def test_shadow_compare_accepts_2024_skipped_and_2025_status_evidence(self) -> None:
        expected_statuses = {
            "matched": "matched,skipped",
            "mismatched": "mismatched,skipped",
            "skipped": "skipped",
        }
        for status, expected in expected_statuses.items():
            with self.subTest(status=status):
                result = validate_mcp_official_rust_sdk_shadow_compare(_shadow_evidence(status=status))

                self.assertEqual(result["shadow_adapter"], "official_rust_sdk")
                self.assertEqual(result["visible_adapter"], "python_legacy")
                self.assertEqual(result["shadow_statuses"], expected)

    def test_shadow_compare_rejects_raw_payload_2024_overclaim_or_bad_transport_version_combination(self) -> None:
        raw = _shadow_evidence(status="matched")
        raw["results"][0]["raw_tool_output"] = "secret"
        with self.assertRaisesRegex(RuntimeError, "mcp_runtime_conformance_blocked"):
            validate_mcp_official_rust_sdk_shadow_compare(raw)

        overclaimed_2024 = _shadow_evidence(status="matched")
        overclaimed_2024["results"][0]["status"] = "matched"
        overclaimed_2024["results"][0].pop("skip_reason")
        with self.assertRaisesRegex(RuntimeError, "mcp_runtime_conformance_blocked"):
            validate_mcp_official_rust_sdk_shadow_compare(overclaimed_2024)

        bad_transport = _shadow_evidence(status="matched")
        bad_transport["results"][0]["transport_family"] = MCP_TRANSPORT_STREAMABLE_HTTP
        with self.assertRaisesRegex(RuntimeError, "mcp_runtime_conformance_blocked"):
            validate_mcp_official_rust_sdk_shadow_compare(bad_transport)

        missing_skip_reason = _shadow_evidence(status="skipped")
        missing_skip_reason["results"][1].pop("skip_reason")
        with self.assertRaisesRegex(RuntimeError, "mcp_runtime_conformance_blocked"):
            validate_mcp_official_rust_sdk_shadow_compare(missing_skip_reason)

        duplicate_version = _shadow_evidence(status="matched")
        duplicate_version["results"].append(dict(duplicate_version["results"][1]))
        with self.assertRaisesRegex(RuntimeError, "mcp_runtime_conformance_blocked"):
            validate_mcp_official_rust_sdk_shadow_compare(duplicate_version)


_REQUIRED_FIELDS = (
    "negotiated_protocol_version",
    "server_info",
    "capabilities",
    "tools_descriptor_shape",
    "safe_tool_call_result_shape",
    "error_category",
)


def _shadow_evidence(*, status: str) -> dict[str, object]:
    versions = (
        (MCP_PROTOCOL_VERSION_2024_11_05, MCP_TRANSPORT_LEGACY_HTTP_SSE),
        (MCP_PROTOCOL_VERSION_2025_03_26, MCP_TRANSPORT_STREAMABLE_HTTP),
        (MCP_PROTOCOL_VERSION_2025_06_18, MCP_TRANSPORT_STREAMABLE_HTTP),
        (MCP_PROTOCOL_VERSION_2025_11_25, MCP_TRANSPORT_STREAMABLE_HTTP),
    )
    results: list[dict[str, object]] = []
    for version, transport in versions:
        item_status = "skipped" if version == MCP_PROTOCOL_VERSION_2024_11_05 else status
        item: dict[str, object] = {
            "server_scope": "fixture_remote_http",
            "protocol_version": version,
            "transport_family": transport,
            "status": item_status,
            "compared_fields": list(_REQUIRED_FIELDS),
            "redaction": {"header_values": "redacted", "raw_payload": "omitted"},
            "visible_path_unchanged": True,
        }
        if item_status == "skipped":
            item["skip_reason"] = (
                "official_sdk_streamable_http_does_not_cover_legacy_http_sse"
                if version == MCP_PROTOCOL_VERSION_2024_11_05
                else "official_sdk_shadow_not_collected_for_test_case"
            )
        results.append(item)
    return {
        "schema_version": "maf.mcp.official_rust_sdk_shadow_compare.v1",
        "visible_adapter": "python_legacy",
        "shadow_adapter": "official_rust_sdk",
        "results": results,
    }


if __name__ == "__main__":
    unittest.main()
