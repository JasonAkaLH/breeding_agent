from __future__ import annotations

import json
import unittest
from pathlib import Path

from src.integrations.mcp.config import MCPServerConfig
from src.integrations.mcp.runtime_state import MCPRuntimeState
from src.integrations.mcp.protocol import (
    DEFAULT_MCP_PROTOCOL_VERSION,
    MCPCompatibilityStatus,
    SUPPORTED_MCP_PROTOCOL_VERSION_ORDER,
    SUPPORTED_MCP_PROTOCOL_VERSIONS,
    is_mcp_transport_family_allowed,
    mcp_feature_status,
    validate_mcp_protocol_version,
)

FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "fixtures" / "mcp" / "messages" / "versions"


class MCPProtocolVersionNegotiationTests(unittest.TestCase):
    def test_supported_versions_are_the_four_client_matrix_versions(self) -> None:
        self.assertEqual(
            SUPPORTED_MCP_PROTOCOL_VERSIONS,
            frozenset({"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"}),
        )
        self.assertEqual(
            SUPPORTED_MCP_PROTOCOL_VERSION_ORDER,
            ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"),
        )
        self.assertEqual(DEFAULT_MCP_PROTOCOL_VERSION, "2025-11-25")
        self.assertEqual(validate_mcp_protocol_version(" 2024-11-05 "), "2024-11-05")
        with self.assertRaisesRegex(ValueError, "Unsupported MCP protocol_version"):
            validate_mcp_protocol_version("2026-01-01")

    def test_transport_family_gate_matches_protocol_generation(self) -> None:
        self.assertTrue(is_mcp_transport_family_allowed("2024-11-05", "legacy_http_sse"))
        self.assertTrue(is_mcp_transport_family_allowed("2024-11-05", "stdio"))
        self.assertFalse(is_mcp_transport_family_allowed("2024-11-05", "streamable_http"))

        for version in ("2025-03-26", "2025-06-18", "2025-11-25"):
            with self.subTest(version=version):
                self.assertTrue(is_mcp_transport_family_allowed(version, "streamable_http"))
                self.assertTrue(is_mcp_transport_family_allowed(version, "stdio"))
                self.assertFalse(is_mcp_transport_family_allowed(version, "legacy_http_sse"))

    def test_feature_gate_keeps_batch_unsupported_and_tools_supported(self) -> None:
        for version in SUPPORTED_MCP_PROTOCOL_VERSION_ORDER:
            with self.subTest(version=version):
                self.assertEqual(mcp_feature_status(version, "ordinary_tools"), MCPCompatibilityStatus.SUPPORTED)
                self.assertEqual(mcp_feature_status(version, "tools/list"), MCPCompatibilityStatus.SUPPORTED)
                self.assertEqual(mcp_feature_status(version, "jsonrpc_batch"), MCPCompatibilityStatus.NOT_SUPPORTED)
                self.assertEqual(mcp_feature_status(version, "batch"), MCPCompatibilityStatus.NOT_SUPPORTED)

    def test_feature_gate_covers_deferred_and_degraded_features(self) -> None:
        for version in SUPPORTED_MCP_PROTOCOL_VERSION_ORDER:
            with self.subTest(version=version):
                self.assertEqual(mcp_feature_status(version, "server_to_client_request"), MCPCompatibilityStatus.COMPATIBLE_DEGRADED)
                self.assertEqual(mcp_feature_status(version, "roots"), MCPCompatibilityStatus.CONFIG_GATED)
                self.assertEqual(mcp_feature_status(version, "sampling"), MCPCompatibilityStatus.CONFIG_GATED)
                expected_future = MCPCompatibilityStatus.NOT_APPLICABLE if version == "2024-11-05" else MCPCompatibilityStatus.FUTURE
                self.assertEqual(mcp_feature_status(version, "resources"), expected_future)
                self.assertEqual(mcp_feature_status(version, "prompts"), expected_future)
                self.assertEqual(mcp_feature_status(version, "tasks"), expected_future)

    def test_config_tracks_pinned_protocol_version_and_rejects_bad_pairs(self) -> None:
        unpinned = MCPServerConfig.from_mapping({"server_id": "crm", "endpoint": "https://mcp.example.com/rpc"})
        self.assertEqual(unpinned.protocol_version, DEFAULT_MCP_PROTOCOL_VERSION)
        self.assertFalse(unpinned.protocol_version_pinned)
        self.assertEqual(unpinned.validation_error(), "")

        pinned_legacy = MCPServerConfig.from_mapping(
            {
                "server_id": "legacy",
                "transport": "legacy_http_sse",
                "endpoint": "https://mcp.example.com/sse",
                "protocol_version": "2024-11-05",
            }
        )
        self.assertTrue(pinned_legacy.protocol_version_pinned)
        self.assertEqual(pinned_legacy.validation_error(), "")

        legacy_over_streamable = MCPServerConfig.from_mapping(
            {
                "server_id": "bad1",
                "transport": "streamable_http",
                "endpoint": "https://mcp.example.com/rpc",
                "protocol_version": "2024-11-05",
            }
        )
        self.assertIn("incompatible", legacy_over_streamable.validation_error())

        streamable_over_legacy = MCPServerConfig.from_mapping(
            {
                "server_id": "bad2",
                "transport": "legacy_http_sse",
                "endpoint": "https://mcp.example.com/sse",
                "protocol_version": "2025-03-26",
            }
        )
        self.assertIn("incompatible", streamable_over_legacy.validation_error())

        unknown_version = MCPServerConfig.from_mapping(
            {
                "server_id": "unknown",
                "endpoint": "https://mcp.example.com/rpc",
                "protocol_version": "2026-01-01",
            }
        )
        self.assertIn("Unsupported MCP protocol_version", unknown_version.validation_error())

    def test_versioned_initialize_fixtures_cover_all_supported_versions(self) -> None:
        discovered = set()
        for version in SUPPORTED_MCP_PROTOCOL_VERSION_ORDER:
            request_path = FIXTURE_ROOT / version / "initialize_request.json"
            result_path = FIXTURE_ROOT / version / "initialize_result.json"
            self.assertTrue(request_path.exists(), f"missing request fixture for {version}")
            self.assertTrue(result_path.exists(), f"missing result fixture for {version}")
            request_payload = json.loads(request_path.read_text(encoding="utf-8"))
            result_payload = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(request_payload["params"]["protocolVersion"], version)
            self.assertEqual(request_payload["params"]["capabilities"], {})
            self.assertEqual(result_payload["result"]["protocolVersion"], version)
            self.assertIn("serverInfo", result_payload["result"])
            discovered.add(version)
        self.assertEqual(discovered, set(SUPPORTED_MCP_PROTOCOL_VERSION_ORDER))

    def test_runtime_skips_optional_negotiation_or_transport_failure_but_fails_required(self) -> None:
        optional_config = {
            "enabled": True,
            "servers": [
                {
                    "server_id": "optional_legacy",
                    "transport": "legacy_http_sse",
                    "endpoint": "https://mcp.example.com/sse",
                    "protocol_version": "2024-11-05",
                    "required": False,
                }
            ],
        }
        optional_state = MCPRuntimeState(config=optional_config)

        optional_result = optional_state.refresh_sync(reason="startup", force=True)

        self.assertEqual(optional_result.status, "completed")
        self.assertEqual(optional_result.registered_count, 0)
        self.assertEqual(optional_state.active_bundle.diagnostics[0].reason, "legacy_sse_connect_failed")
        self.assertEqual(optional_state.active_bundle.diagnostics[0].requested_protocol_version, "2024-11-05")
        self.assertEqual(optional_state.active_bundle.diagnostics[0].transport_family, "legacy_http_sse")
        self.assertFalse(optional_state.active_bundle.diagnostics[0].required)

        required_config = {
            "enabled": True,
            "servers": [
                {
                    "server_id": "required_legacy",
                    "transport": "legacy_http_sse",
                    "endpoint": "https://mcp.example.com/sse",
                    "protocol_version": "2024-11-05",
                    "required": True,
                }
            ],
        }
        required_state = MCPRuntimeState(config=required_config)

        required_pending = required_state.prepare_refresh_sync(reason="startup", force=True)
        required_result = required_pending.result

        self.assertEqual(required_result.status, "failed")
        self.assertEqual(required_result.error_type, "MCPClientError")
        self.assertEqual(required_pending.bundle.diagnostics[0].reason, "legacy_sse_connect_failed")
        self.assertEqual(required_pending.bundle.diagnostics[0].requested_protocol_version, "2024-11-05")
        self.assertEqual(required_pending.bundle.diagnostics[0].transport_family, "legacy_http_sse")
        self.assertTrue(required_pending.bundle.diagnostics[0].required)
        self.assertEqual(required_state.last_refresh_diagnostics[0].server_id, "required_legacy")


if __name__ == "__main__":
    unittest.main()
