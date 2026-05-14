from __future__ import annotations

import unittest

from src.integrations.mcp.config import MCPRuntimeConfig
from src.integrations.mcp.promotion import MCPEnforcePromotionGate, MCPShadowMetrics, can_shadow_replay_tool
from src.integrations.mcp.runtime_state import MCPRuntimeState
from src.integrations.mcp.sidecar import (
    MCP_SIDECAR_COMPONENT,
    MCP_SIDECAR_PROTOCOL_VERSION,
    MCPSidecarVersionInfo,
    InMemoryMCPSidecarTransport,
    MCPSidecarClient,
)
from src.integrations.mcp.protocol import MCP_PROTOCOL_VERSION


class MCPPhase5ShadowEnforceGateTests(unittest.IsolatedAsyncioTestCase):
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
                },
            }
        )
        state = MCPRuntimeState(config=config, sidecar_client=sidecar_client)

        result = await state.refresh(reason="startup", force=True)

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error_type, "MCPSidecarCompatibilityError")

    async def test_enforce_runtime_fails_closed_when_sidecar_lacks_canonical_runtime_operations(self) -> None:
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
                },
            }
        )
        state = MCPRuntimeState(config=config, sidecar_client=sidecar_client)

        result = await state.refresh(reason="startup", force=True)

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error_type, "MCPSidecarRuntimeUnavailable")


if __name__ == "__main__":
    unittest.main()
