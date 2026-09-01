from __future__ import annotations

import json
import unittest
from pathlib import Path

from src.integrations.mcp.config import MCPServerConfig
from src.integrations.mcp.client import MCPClient
from src.integrations.mcp.runtime_state import MCPRuntimeState
from src.integrations.mcp.protocol import (
    DEFAULT_MCP_PROTOCOL_VERSION,
    MCPCompatibilityStatus,
    SUPPORTED_MCP_PROTOCOL_VERSION_ORDER,
    SUPPORTED_MCP_PROTOCOL_VERSIONS,
    is_mcp_transport_family_allowed,
    mcp_feature_status,
    normalize_json_rpc_response_id,
    validate_mcp_protocol_version,
)

FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "fixtures" / "mcp" / "messages" / "versions"
MODERN_FIXTURE_ROOT = FIXTURE_ROOT.parent / "2026-07-28"


class MCPProtocolVersionNegotiationTests(unittest.TestCase):
    def test_response_id_normalizer_accepts_exact_and_one_way_integer_aliases(self) -> None:
        exact_integer = {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}
        exact_string = {"jsonrpc": "2.0", "id": "1", "result": {"ok": True}}
        alias = {"jsonrpc": "2.0", "id": "-7", "result": {"ok": True}}

        self.assertIs(
            normalize_json_rpc_response_id(exact_integer, expected_request_id=1),
            exact_integer,
        )
        self.assertIs(
            normalize_json_rpc_response_id(exact_string, expected_request_id="1"),
            exact_string,
        )
        normalized = normalize_json_rpc_response_id(
            alias,
            expected_request_id=-7,
        )
        self.assertIsNot(normalized, alias)
        self.assertEqual(normalized, {"jsonrpc": "2.0", "id": -7, "result": {"ok": True}})
        self.assertEqual(alias["id"], "-7")

        for expected in (0, 9):
            with self.subTest(expected=expected):
                message = {"jsonrpc": "2.0", "id": str(expected), "error": {"code": -1}}
                self.assertEqual(
                    normalize_json_rpc_response_id(
                        message,
                        expected_request_id=expected,
                    )["id"],
                    expected,
                )

    def test_response_id_normalizer_rejects_reverse_noncanonical_and_loose_types(self) -> None:
        rejected = (
            ("1", 1),
            (1, True),
            (1, 1.0),
            (1, None),
            (1, "01"),
            (1, "+1"),
            (1, " 1"),
            (1, "1.0"),
            (1, "1e0"),
            (1, "١"),
            (1, []),
            (1, {}),
            (1, "2"),
        )
        for expected, raw in rejected:
            with self.subTest(expected=expected, raw=raw):
                message = {"jsonrpc": "2.0", "id": raw, "result": {}}
                self.assertIsNone(
                    normalize_json_rpc_response_id(
                        message,
                        expected_request_id=expected,
                    )
                )

        for message in (
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            {"jsonrpc": "2.0", "method": "notifications/progress"},
            {"jsonrpc": "1.0", "id": 1, "result": {}},
        ):
            with self.subTest(message=message):
                self.assertIsNone(
                    normalize_json_rpc_response_id(
                        message,
                        expected_request_id=1,
                    )
                )

    def test_supported_versions_are_the_five_client_matrix_versions(self) -> None:
        self.assertEqual(
            SUPPORTED_MCP_PROTOCOL_VERSIONS,
            frozenset({"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25", "2026-07-28"}),
        )
        self.assertEqual(
            SUPPORTED_MCP_PROTOCOL_VERSION_ORDER,
            ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25", "2026-07-28"),
        )
        self.assertEqual(DEFAULT_MCP_PROTOCOL_VERSION, "2025-11-25")
        self.assertEqual(validate_mcp_protocol_version(" 2024-11-05 "), "2024-11-05")
        with self.assertRaisesRegex(ValueError, "Unsupported MCP protocol_version"):
            validate_mcp_protocol_version("2026-01-01")

    def test_transport_family_gate_matches_protocol_generation(self) -> None:
        self.assertTrue(is_mcp_transport_family_allowed("2024-11-05", "legacy_http_sse"))
        self.assertTrue(is_mcp_transport_family_allowed("2024-11-05", "stdio"))
        self.assertFalse(is_mcp_transport_family_allowed("2024-11-05", "streamable_http"))

        for version in ("2025-03-26", "2025-06-18", "2025-11-25", "2026-07-28"):
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
                expected_server_request = (
                    MCPCompatibilityStatus.NOT_SUPPORTED
                    if version == "2026-07-28"
                    else MCPCompatibilityStatus.COMPATIBLE_DEGRADED
                )
                self.assertEqual(mcp_feature_status(version, "server_to_client_request"), expected_server_request)
                expected_deprecated = (
                    MCPCompatibilityStatus.NOT_SUPPORTED
                    if version == "2026-07-28"
                    else MCPCompatibilityStatus.CONFIG_GATED
                )
                self.assertEqual(mcp_feature_status(version, "roots"), expected_deprecated)
                self.assertEqual(mcp_feature_status(version, "sampling"), expected_deprecated)
                expected_future = MCPCompatibilityStatus.NOT_APPLICABLE if version == "2024-11-05" else MCPCompatibilityStatus.FUTURE
                self.assertEqual(mcp_feature_status(version, "resources"), expected_future)
                self.assertEqual(mcp_feature_status(version, "prompts"), expected_future)
                expected_tasks = MCPCompatibilityStatus.CONFIG_GATED if version == "2026-07-28" else expected_future
                self.assertEqual(mcp_feature_status(version, "tasks"), expected_tasks)

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

    def test_versioned_initialize_fixtures_cover_initialization_based_versions(self) -> None:
        discovered = set()
        for version in SUPPORTED_MCP_PROTOCOL_VERSION_ORDER[:-1]:
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
        self.assertEqual(discovered, set(SUPPORTED_MCP_PROTOCOL_VERSION_ORDER[:-1]))

    def test_2026_fixture_uses_server_discover_instead_of_initialize(self) -> None:
        request_payload = json.loads((MODERN_FIXTURE_ROOT / "server_discover_request.json").read_text(encoding="utf-8"))

        self.assertEqual(request_payload["method"], "server/discover")
        self.assertEqual(
            request_payload["params"]["_meta"]["io.modelcontextprotocol/protocolVersion"],
            "2026-07-28",
        )
        self.assertFalse((MODERN_FIXTURE_ROOT / "initialize_request.json").exists())

    def test_session_era_client_rejects_2026_revision(self) -> None:
        with self.assertRaisesRegex(ValueError, "MCP2026Adapter"):
            MCPClient(server_id="modern", transport=object(), protocol_version="2026-07-28")

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
