from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any

from scripts.validate_prd06_safety_kernel_evidence import validate_evidence
from src.integrations.rust_safety_contract import load_safety_contract


class PRD06SafetyKernelEvidenceTest(unittest.TestCase):
    def test_pending_ledger_is_allowed_only_with_allow_pending(self) -> None:
        evidence_path = Path("docs/prd/rust/evidence/prd06/safety_kernel_release_gates.json")

        allowed = subprocess.run(
            [
                sys.executable,
                "-S",
                "scripts/validate_prd06_safety_kernel_evidence.py",
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
            [sys.executable, "scripts/validate_prd06_safety_kernel_evidence.py", "--evidence", str(evidence_path)],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        self.assertEqual(json.loads(allowed.stdout)["status"], "pending")
        self.assertNotEqual(strict.returncode, 0)
        self.assertIn("prd06_safety_kernel_evidence_pending", strict.stderr)

    def test_complete_synthetic_evidence_validates_all_prd06_gates(self) -> None:
        contract = load_safety_contract()
        payload = {
            "schema_version": "maf.prd06.safety_kernel_evidence.v1",
            "status": "ready",
            "last_updated": "2026-05-17",
            "artifact_provenance": _artifact(contract),
            "allowed_artifact_checksums": ["sha256:safety-wheel"],
            "allowed_cargo_lock_digests": ["sha256:cargo-lock"],
            "fuzz_report": _fuzz_report(),
            "coverage_report": _coverage_report(),
            "benchmark_report": _benchmark_report(),
            "promotion_readiness": _promotion_readiness(),
            "ops_readiness": _ops_readiness(),
            "decommission_readiness": _decommission_readiness(),
            "blockers": [],
        }

        result = validate_evidence(payload, allow_pending=False)

        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["pending_gates"], [])
        self.assertEqual(result["results"]["artifact_provenance"]["artifact_kind"], "safety_kernels_pyo3_wheel")


def _artifact(contract: dict[str, Any]) -> dict[str, str]:
    return {
        "source": "ci_pipeline",
        "artifact_kind": "safety_kernels_pyo3_wheel",
        "checksum_sha256": "sha256:safety-wheel",
        "cargo_lock_digest": "sha256:cargo-lock",
        "contract_version": contract["contract_version"],
        "schema_hash": contract["schema_hash"],
        "error_code_table_hash": contract["error_code_table_hash"],
        "sbom_digest": "sha256:sbom",
        "provenance_attestation": "sha256:provenance",
    }


def _fuzz_report() -> dict[str, Any]:
    return {
        "bounded_smoke_seconds": 30,
        "targets": {
            "artifact_path": True,
            "auth_core": True,
            "audit_sanitizer": True,
            "data_access_readonly": True,
        },
        "crash_count": 0,
        "leak_count": 0,
    }


def _coverage_report() -> dict[str, Any]:
    return {
        "line_coverage": {
            "maf_artifact_store": 90,
            "maf_auth_core": 90,
            "maf_data_access": 90,
            "maf_audit_sanitizer": 90,
        }
    }


def _benchmark_report() -> dict[str, Any]:
    operations = (
        "artifact_path_normalization",
        "archive_safety",
        "artifact_hash",
        "auth_primitive",
        "readonly_row_shaping",
        "audit_redaction",
    )
    metrics = {
        "p50_ms": 1,
        "p95_ms": 2,
        "p99_ms": 3,
        "cpu_percent": 10,
        "memory_mb": 64,
        "payload_bytes": 128,
        "result_bytes": 64,
    }
    return {
        baseline: {operation: dict(metrics) for operation in operations}
        for baseline in ("python_legacy", "rust_kernel")
    }


def _promotion_readiness() -> dict[str, Any]:
    return {
        "scope": "artifact_auth_data_access_audit",
        "shadow_days": 7,
        "shadow_samples": 1000,
        "contract_mismatch_count": 0,
        "secret_leak_count": 0,
        "path_escape_count": 0,
        "readonly_policy_bypass_count": 0,
        "redaction_failure_count": 0,
        "python_legacy_p95_ms": 100,
        "rust_kernel_p95_ms": 100,
        "python_legacy_error_rate_ppm": 1,
        "rust_error_rate_ppm": 1,
        "evidence": {
            "artifact_quarantine_drill_passed": True,
            "secret_rotation_drill_passed": True,
            "db_limit_drill_passed": True,
            "restore_drill_passed": True,
            "shadow_side_effect_safety_passed": True,
        },
    }


def _ops_readiness() -> dict[str, Any]:
    return {
        "observability": {
            "health_dashboard": True,
            "slo_dashboard": True,
            "structured_metrics": True,
        },
        "alerts": {
            "artifact_quarantine": True,
            "secret_rotation_failure": True,
            "identity_mismatch": True,
            "redaction_failure": True,
            "db_limit_failure": True,
            "archive_timeout": True,
        },
        "drills": {
            "artifact_quarantine": True,
            "secret_rotation": True,
            "identity_mismatch": True,
            "redaction_failure": True,
            "db_limit_failure": True,
            "restore": True,
        },
    }


def _decommission_readiness() -> dict[str, Any]:
    return {
        "canonical_safety_kernels_stable": True,
        "rollback_path": "rust_safety_kernel_mode_flags",
        "legacy_paths_removed": {
            "python_path_sanitizer_canonical_logic": True,
            "python_auth_primitive_canonical_logic": True,
            "python_readonly_db_policy_canonical_logic": True,
            "python_audit_sanitizer_canonical_logic": True,
        },
        "facade_only_paths": {
            "artifact_store_facade": True,
            "auth_service_facade": True,
            "mysql_readonly_facade": True,
            "audit_sink_facade": True,
        },
        "evidence": {
            "architecture_guard": True,
            "rollback_path": True,
            "owner_signoff": True,
        },
    }


if __name__ == "__main__":
    unittest.main()
