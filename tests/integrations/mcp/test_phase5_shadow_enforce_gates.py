from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.integrations.mcp.config import MCPRuntimeConfig
from src.integrations.mcp.promotion import MCPEnforcePromotionGate, MCPShadowMetrics, can_shadow_replay_tool
from src.integrations.mcp.runtime_state import MCPRuntimeState
from src.integrations.mcp.rust_contract import load_mcp_runtime_contract
from src.integrations.mcp.sidecar import (
    MCP_SIDECAR_COMPONENT,
    MCP_SIDECAR_PROTOCOL_VERSION,
    MCPSidecarVersionInfo,
    InMemoryMCPSidecarTransport,
    MCPSidecarClient,
)
from src.integrations.mcp.protocol import MCP_PROTOCOL_VERSION


class MCPPhase5ShadowEnforceGateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmpdir.name)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_promotion_gate_requires_all_shadow_enforce_evidence(self) -> None:
        gate = MCPEnforcePromotionGate()
        failing = gate.evaluate(
            MCPShadowMetrics(
                consecutive_shadow_days=6,
                effective_samples=999,
                contract_mismatch_count=1,
                panic_or_crash_count=1,
                raw_leak_count=1,
                rust_p95_latency_ms=111,
                legacy_p95_latency_ms=100,
                rust_error_rate=0.02,
                legacy_error_rate=0.01,
            )
        )
        self.assertFalse(failing.allowed)
        self.assertIn("shadow_duration_below_threshold", failing.blockers)
        self.assertIn("mcp_conformance_missing", failing.blockers)

        passing = gate.evaluate(
            MCPShadowMetrics(
                consecutive_shadow_days=7,
                effective_samples=1000,
                rust_p95_latency_ms=100,
                legacy_p95_latency_ms=100,
                rust_error_rate=0.01,
                legacy_error_rate=0.01,
                recovery_drill_passed=True,
                rollback_drill_passed=True,
                ops_ready=True,
                conformance_passed=True,
            )
        )
        self.assertTrue(passing.allowed)
        self.assertEqual(passing.blockers, ())

    def test_shadow_replay_is_denied_for_side_effecting_tools_without_dry_run_or_idempotency(self) -> None:
        self.assertTrue(can_shadow_replay_tool(risk_level="read_only"))
        self.assertTrue(can_shadow_replay_tool(risk_level="write", dry_run=True))
        self.assertTrue(can_shadow_replay_tool(risk_level="write", idempotent=True))
        self.assertFalse(can_shadow_replay_tool(risk_level="write"))

    async def test_enforce_runtime_fails_closed_on_incompatible_sidecar(self) -> None:
        manifest_path, allowlist_path = self._write_mcp_sidecar_artifact_trust_files()
        version = MCPSidecarVersionInfo(
            component=MCP_SIDECAR_COMPONENT,
            build_version="test",
            protocol_version=MCP_SIDECAR_PROTOCOL_VERSION,
            schema_hash="schema-v1",
            error_code_table_hash="errors-v1",
            supported_features=frozenset({"short_call"}),
            min_client_version="0.1.0",
            max_client_version="0.1.x",
            external_mcp_protocol_version=MCP_PROTOCOL_VERSION,
        )
        sidecar_client = MCPSidecarClient(
            transport=InMemoryMCPSidecarTransport(version=version),
            expected_schema_hash="schema-v1",
            expected_error_code_table_hash="errors-v1",
        )
        config = MCPRuntimeConfig.from_mapping(
            {
                "enabled": True,
                "rust_runtime": {
                    "mode": "enforce",
                    "endpoint": "unix:///tmp/maf-mcp.sock",
                    "required_features": ["mcp_tasks"],
                    "artifact_manifest_path": str(manifest_path),
                    "artifact_allowlist_path": str(allowlist_path),
                },
            }
        )
        state = MCPRuntimeState(config=config, sidecar_client=sidecar_client)

        result = await state.refresh(reason="startup", force=True)

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error_type, "MCPSidecarCompatibilityError")

    async def test_enforce_runtime_fails_closed_when_sidecar_lacks_canonical_runtime_operations(self) -> None:
        manifest_path, allowlist_path = self._write_mcp_sidecar_artifact_trust_files()
        version = MCPSidecarVersionInfo(
            component=MCP_SIDECAR_COMPONENT,
            build_version="test",
            protocol_version=MCP_SIDECAR_PROTOCOL_VERSION,
            schema_hash="schema-v1",
            error_code_table_hash="errors-v1",
            supported_features=frozenset({"health", "readiness", "version", "compatibility_handshake"}),
            min_client_version="0.1.0",
            max_client_version="0.1.x",
            external_mcp_protocol_version=MCP_PROTOCOL_VERSION,
        )
        sidecar_client = MCPSidecarClient(
            transport=InMemoryMCPSidecarTransport(version=version),
            expected_schema_hash="schema-v1",
            expected_error_code_table_hash="errors-v1",
        )
        config = MCPRuntimeConfig.from_mapping(
            {
                "enabled": True,
                "rust_runtime": {
                    "mode": "enforce",
                    "endpoint": "unix:///tmp/maf-mcp.sock",
                    "required_features": ["health"],
                    "artifact_manifest_path": str(manifest_path),
                    "artifact_allowlist_path": str(allowlist_path),
                },
            }
        )
        state = MCPRuntimeState(config=config, sidecar_client=sidecar_client)

        result = await state.refresh(reason="startup", force=True)

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error_type, "MCPSidecarRuntimeUnavailable")

    def _write_mcp_sidecar_artifact_trust_files(self) -> tuple[Path, Path]:
        contract = load_mcp_runtime_contract()
        manifest_payload = {
            "schema_version": "maf.rust_artifact_provenance.v1",
            "component": "maf_mcp_runtime",
            "artifact_id": "maf_mcp_runtime_sidecar",
            "artifact_kind": "sidecar_binary",
            "artifact_name": "maf-mcp-runtime-sidecar-linux-x86_64",
            "artifact_sha256": "sha256:mcp-sidecar",
            "cargo_lock_sha256": "sha256:cargo-lock",
            "sbom_sha256": "sha256:sbom",
            "provenance_sha256": "sha256:provenance",
            "source": "ci_pipeline",
            "git_commit": "abcdef123456",
            "toolchain": "rustc 1.95.0",
            "target_triple": "x86_64-unknown-linux-gnu",
            "build_profile": "release",
            "cargo_features": ["default"],
            "contract_hashes": {
                "mcp_runtime": contract["schema_hash"],
                "mcp_runtime_errors": contract["error_code_table_hash"],
            },
            "proto_hashes": {"mcp": "maf_mcp_proto_v1_20260517"},
        }
        manifest_path = self.workspace / "mcp-sidecar.manifest.json"
        allowlist_path = self.workspace / "mcp-sidecar.allowlist.json"
        manifest_path.write_text(json.dumps(manifest_payload), encoding="utf-8")
        allowlist_path.write_text(
            json.dumps({"schema_version": "maf.rust_artifact_allowlist.v1", "allowed_artifacts": [manifest_payload]}),
            encoding="utf-8",
        )
        return manifest_path, allowlist_path


if __name__ == "__main__":
    unittest.main()
