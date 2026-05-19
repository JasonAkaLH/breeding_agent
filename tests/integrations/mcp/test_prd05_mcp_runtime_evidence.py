from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any

from scripts.validate_prd05_mcp_runtime_evidence import validate_evidence
from src.integrations.mcp.mcp_runtime_gates import (
    validate_mcp_enforce_allowlist,
    validate_mcp_official_rust_sdk_shadow_compare,
    validate_mcp_runtime_conformance_report,
)
from src.integrations.mcp.protocol import (
    MCP_PROTOCOL_VERSION_2024_11_05,
    MCP_PROTOCOL_VERSION_2025_03_26,
    MCP_PROTOCOL_VERSION_2025_06_18,
    MCP_PROTOCOL_VERSION_2025_11_25,
    MCP_TRANSPORT_LEGACY_HTTP_SSE,
    MCP_TRANSPORT_STREAMABLE_HTTP,
    SUPPORTED_MCP_PROTOCOL_VERSION_ORDER,
)
from src.integrations.mcp.rust_contract import load_mcp_runtime_contract


class PRD05MCPRuntimeEvidenceTest(unittest.TestCase):
    def test_pending_ledger_is_allowed_only_with_allow_pending(self) -> None:
        evidence_path = Path("docs/prd/rust/evidence/prd05/mcp_runtime_release_gates.json")

        allowed = subprocess.run(
            [
                sys.executable,
                "-S",
                "scripts/validate_prd05_mcp_runtime_evidence.py",
                "--evidence",
                str(evidence_path),
                "--allow-pending",
                "--json",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        strict = subprocess.run(
            [sys.executable, "scripts/validate_prd05_mcp_runtime_evidence.py", "--evidence", str(evidence_path)],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        self.assertEqual(json.loads(allowed.stdout)["status"], "pending")
        self.assertNotEqual(strict.returncode, 0)
        self.assertIn("prd05_mcp_runtime_evidence_pending", strict.stderr)

    def test_pending_ledger_records_repo_local_client_compatibility_conformance(self) -> None:
        evidence_path = Path("docs/prd/rust/evidence/prd05/mcp_runtime_release_gates.json")
        payload = json.loads(evidence_path.read_text(encoding="utf-8"))

        conformance_report = payload["conformance_report"]
        self.assertEqual(conformance_report["status"], "pending")
        self.assertEqual(
            conformance_report["expected_schema_version"],
            "maf.mcp.client_compatibility_conformance.v1",
        )
        repo_local = conformance_report["repo_local_client_compatibility_conformance"]

        result = validate_mcp_runtime_conformance_report(repo_local)

        self.assertEqual(
            result["supported_mcp_spec_versions"],
            ",".join(SUPPORTED_MCP_PROTOCOL_VERSION_ORDER),
        )
        self.assertEqual(repo_local["evidence_kind"], "repo_local_client_compatibility")
        self.assertEqual(repo_local["visible_runtime"], "python_mcp_client_path")
        self.assertFalse(repo_local["sidecar_canonical_multi_version_transport"])
        self.assertIn("not Rust sidecar production readiness", repo_local["phase_results_scope"])
        self.assertEqual(repo_local["transport_scope"], "remote_http_only_until_stdio_sandbox_passes")
        self.assertFalse(repo_local["stdio_sandbox_conformance_passed"])
        self.assertEqual(repo_local["adapters"], ["python_legacy", "official_rust_sdk"])
        shadow = validate_mcp_official_rust_sdk_shadow_compare(repo_local["official_rust_sdk_shadow_compare"])
        self.assertEqual(shadow["shadow_statuses"], "matched,skipped")
        allowlist = validate_mcp_enforce_allowlist(repo_local["adapter_enforce_allowlist"])
        self.assertEqual(allowlist["enforce_allowed_combinations"], "0")
        sdk_dependency = repo_local["official_rust_sdk_dependency"]
        self.assertEqual(sdk_dependency["crate"], "rmcp")
        self.assertEqual(sdk_dependency["version"], "1.7.0")
        self.assertEqual(sdk_dependency["license"], "Apache-2.0")
        self.assertFalse(sdk_dependency["default_features"])
        self.assertEqual(
            sdk_dependency["features"],
            ["client", "transport-streamable-http-client-reqwest", "reqwest"],
        )
        self.assertFalse(sdk_dependency["server_macros_enabled"])
        self.assertFalse(sdk_dependency["production_enforce_enabled"])
        self.assertFalse(repo_local["external_smoke_samples"]["is_normative"])
        self.assertFalse(payload["client_compatibility"]["production_release_gate_satisfied"])
        self.assertEqual(payload["client_compatibility"]["official_rust_sdk_dependency"], sdk_dependency)
        self.assertIn("2024-11-05 legacy_http_sse remains python_legacy", payload["client_compatibility"]["official_rust_sdk_transport_scope"])
        self.assertGreaterEqual(len(repo_local["verification_commands"]), 4)

    def test_complete_synthetic_evidence_validates_all_prd05_gates(self) -> None:
        contract = load_mcp_runtime_contract()
        payload = {
            "schema_version": "maf.prd05.mcp_runtime_evidence.v1",
            "status": "ready",
            "last_updated": "2026-05-17",
            "artifact_provenance": _artifact(contract),
            "allowed_artifact_checksums": ["sha256:mcp-sidecar"],
            "allowed_cargo_lock_digests": ["sha256:cargo-lock"],
            "conformance_report": _conformance(),
            "benchmark_report": _benchmark(),
            "promotion_readiness": _promotion(),
            "ops_readiness": _ops(),
            "recovery_readiness": _recovery(),
            "decommission_readiness": _decommission(),
            "blockers": [],
        }

        result = validate_evidence(payload, allow_pending=False)

        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["pending_gates"], [])
        self.assertEqual(result["results"]["artifact_provenance"]["artifact_kind"], "mcp_runtime_sidecar_binary")
        self.assertEqual(
            result["results"]["conformance_report"]["supported_mcp_spec_versions"],
            ",".join(SUPPORTED_MCP_PROTOCOL_VERSION_ORDER),
        )


def _artifact(contract: dict[str, Any]) -> dict[str, str]:
    return {
        "source": "ci_pipeline",
        "artifact_kind": "mcp_runtime_sidecar_binary",
        "checksum_sha256": "sha256:mcp-sidecar",
        "cargo_lock_digest": "sha256:cargo-lock",
        "protocol_version": contract["protocol_version"],
        "schema_hash": contract["schema_hash"],
        "error_code_table_hash": contract["error_code_table_hash"],
        "proto_hash": "maf_mcp_proto_v1_20260517",
        "sbom_digest": "sha256:sbom",
        "provenance_attestation": "sha256:provenance",
    }


def _conformance() -> dict[str, Any]:
    return {
        "schema_version": "maf.mcp.client_compatibility_conformance.v1",
        "supported_mcp_spec_versions": list(SUPPORTED_MCP_PROTOCOL_VERSION_ORDER),
        "phase_results": {
            "phase_0": True,
            "phase_1": True,
            "phase_2": True,
            "phase_3": True,
            "phase_4": True,
            "phase_5": True,
        },
        "version_results": {
            MCP_PROTOCOL_VERSION_2024_11_05: _version_result(MCP_TRANSPORT_LEGACY_HTTP_SSE),
            MCP_PROTOCOL_VERSION_2025_03_26: _version_result(MCP_TRANSPORT_STREAMABLE_HTTP),
            MCP_PROTOCOL_VERSION_2025_06_18: _version_result(MCP_TRANSPORT_STREAMABLE_HTTP),
            MCP_PROTOCOL_VERSION_2025_11_25: _version_result(MCP_TRANSPORT_STREAMABLE_HTTP),
        },
        "jsonrpc_batch_rejected": True,
        "raw_id_redaction_passed": True,
        "safe_diagnostics_passed": True,
    }


def _version_result(transport_family: str) -> dict[str, Any]:
    return {
        "initialize": True,
        "transport_family": transport_family,
        "transport": True,
        "tools_list": True,
        "tools_call": True,
        "batch_rejected": True,
        "raw_id_redaction_passed": True,
        "safe_diagnostics_passed": True,
    }


def _benchmark() -> dict[str, Any]:
    operations = (
        "initialize",
        "list_tools",
        "call_tool",
        "sse_stream",
        "task_result",
        "output_sanitizer",
        "bundle_activation",
    )
    metrics = {
        "p50_ms": 1,
        "p95_ms": 2,
        "p99_ms": 3,
        "cpu_percent": 10,
        "memory_mb": 64,
        "raw_output_bytes": 128,
        "sanitized_output_bytes": 64,
    }
    return {
        baseline: {operation: dict(metrics) for operation in operations}
        for baseline in ("python_legacy", "rust_sidecar")
    }


def _promotion() -> dict[str, Any]:
    return {
        "scope": "mcp_runtime",
        "shadow_days": 7,
        "shadow_samples": 1000,
        "contract_mismatch_count": 0,
        "panic_or_crash_count": 0,
        "raw_leak_count": 0,
        "identity_mismatch_count": 0,
        "python_legacy_p95_ms": 100,
        "rust_sidecar_p95_ms": 100,
        "python_legacy_error_rate_ppm": 1,
        "rust_error_rate_ppm": 1,
        "evidence": {
            "conformance_passed": True,
            "recovery_drill_passed": True,
            "rollback_drill_passed": True,
            "ops_ready": True,
            "shadow_side_effect_safety_passed": True,
        },
    }


def _ops() -> dict[str, Any]:
    return {
        "observability": {
            "health_dashboard": True,
            "readiness_dashboard": True,
            "slo_dashboard": True,
            "structured_metrics": True,
        },
        "alerts": {
            "sidecar_unavailable": True,
            "external_server_unavailable": True,
            "stream_idle_or_reconnect": True,
            "registry_write_failure": True,
            "sanitizer_or_redaction_failure": True,
            "bundle_quarantine": True,
        },
        "drills": {
            "drain_restart": True,
            "registry_restore": True,
            "bundle_rollback": True,
            "identity_failure": True,
        },
    }


def _recovery() -> dict[str, Any]:
    return {
        "evidence": {
            "migration_lock": True,
            "backup": True,
            "restore": True,
            "replay_check": True,
            "rollback": True,
            "roll_forward": True,
        }
    }


def _decommission() -> dict[str, Any]:
    return {
        "canonical_mcp_runtime_stable": True,
        "rollback_path": "legacy_mcp_runtime_flag",
        "legacy_paths_removed": {
            "python_jsonrpc_canonical_parser": True,
            "python_sse_canonical_router": True,
            "python_output_sanitizer_canonical_logic": True,
            "python_bundle_activation_canonical_logic": True,
            "python_long_task_registry_production_path": True,
        },
        "facade_only_paths": {
            "mcp_executor_facade": True,
            "mcp_sidecar_client": True,
            "capability_descriptor_sync": True,
        },
        "evidence": {
            "architecture_guard": True,
            "rollback_path": True,
            "owner_signoff": True,
        },
    }


if __name__ == "__main__":
    unittest.main()
