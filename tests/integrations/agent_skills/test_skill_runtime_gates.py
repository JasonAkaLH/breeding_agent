from __future__ import annotations

import unittest
from typing import Any

from src.integrations.agent_skills.rust_contract import error_policy, load_skill_runtime_contract
from src.integrations.agent_skills.skill_runtime_gates import (
    validate_skill_runtime_artifact_provenance,
    validate_skill_runtime_benchmark_report,
    validate_skill_runtime_decommission_readiness,
    validate_skill_runtime_ops_readiness,
    validate_skill_runtime_promotion_readiness,
)


class SkillRuntimeProductionGateTest(unittest.TestCase):
    def test_artifact_provenance_requires_prebuilt_allowlisted_rust_artifact(self) -> None:
        contract = load_skill_runtime_contract()
        metadata = {
            "source": "ci_pipeline",
            "artifact_kind": "skill_policy_pyo3_wheel",
            "checksum_sha256": "sha256:skill-policy",
            "cargo_lock_digest": "cargo-lock:skill-runtime",
            "contract_version": contract["contract_version"],
            "bundle_revision": "skill-runtime-20260515.1",
            "schema_hash": contract["schema_hash"],
            "sbom_digest": "sbom:skill-runtime",
            "provenance_attestation": "slsa:intoto",
        }

        result = validate_skill_runtime_artifact_provenance(
            metadata,
            allowed_checksums={"sha256:skill-policy"},
            allowed_cargo_lock_digests={"cargo-lock:skill-runtime"},
        )

        self.assertEqual(result["artifact_kind"], "skill_policy_pyo3_wheel")
        self.assertEqual(result["contract_version"], contract["contract_version"])
        self.assertEqual(result["provenance_attestation"], "configured")
        self.assertEqual(result["sbom"], "configured")

    def test_artifact_provenance_fails_closed_on_checksum_mismatch(self) -> None:
        metadata = _valid_artifact_metadata()
        metadata["checksum_sha256"] = "sha256:tampered"

        with self.assertRaisesRegex(RuntimeError, "skill_runtime_artifact_untrusted"):
            validate_skill_runtime_artifact_provenance(
                metadata,
                allowed_checksums={"sha256:skill-policy"},
                allowed_cargo_lock_digests={"cargo-lock:skill-runtime"},
            )

    def test_benchmark_report_requires_python_and_rust_skill_runtime_baselines(self) -> None:
        result = validate_skill_runtime_benchmark_report(_valid_benchmark_report())

        self.assertEqual(result["baselines"], "python_legacy,rust_skill_runtime")
        self.assertIn("sandbox_execution", result["operations"])
        self.assertIn("queue_wait_ms", result["metrics"])

    def test_benchmark_report_fails_closed_when_required_metric_missing(self) -> None:
        report = _valid_benchmark_report()
        del report["rust_skill_runtime"]["sandbox_execution"]["p99_ms"]

        with self.assertRaisesRegex(RuntimeError, "skill_runtime_benchmark_invalid"):
            validate_skill_runtime_benchmark_report(report)

    def test_promotion_readiness_requires_shadow_thresholds_and_evidence(self) -> None:
        result = validate_skill_runtime_promotion_readiness(_valid_promotion_report())

        self.assertEqual(result["promotion"], "ready")
        self.assertEqual(result["scope"], "skill_sandbox")
        self.assertEqual(result["shadow_days"], "7")
        self.assertEqual(result["shadow_samples"], "1000")

    def test_promotion_readiness_blocks_latency_regression(self) -> None:
        report = _valid_promotion_report()
        report["rust_p95_ms"] = 112

        with self.assertRaisesRegex(RuntimeError, "skill_runtime_promotion_blocked"):
            validate_skill_runtime_promotion_readiness(report)

    def test_ops_readiness_requires_observability_runbooks_and_fault_drills(self) -> None:
        result = validate_skill_runtime_ops_readiness(_valid_ops_report())

        self.assertEqual(result["ops"], "ready")
        self.assertIn("artifact_quarantine", result["runbooks"])
        self.assertIn("process_cleanup_failure", result["drills"])

    def test_ops_readiness_fails_closed_when_drill_missing(self) -> None:
        report = _valid_ops_report()
        report["drills"]["process_cleanup_failure"] = False

        with self.assertRaisesRegex(RuntimeError, "skill_runtime_ops_readiness_blocked"):
            validate_skill_runtime_ops_readiness(report)

    def test_decommission_readiness_requires_legacy_python_paths_removed(self) -> None:
        result = validate_skill_runtime_decommission_readiness(_valid_decommission_report())

        self.assertEqual(result["decommission"], "ready")
        self.assertEqual(result["rollback_path"], "deployment_rollback")
        self.assertIn("python_trust_gate", result["removed_legacy_paths"])
        self.assertIn("python_facade", result["facade_only_paths"])

    def test_decommission_readiness_fails_closed_when_legacy_path_not_removed(self) -> None:
        report = _valid_decommission_report()
        report["legacy_paths_removed"]["python_subprocess_sandbox_policy"] = False

        with self.assertRaisesRegex(RuntimeError, "skill_runtime_decommission_blocked"):
            validate_skill_runtime_decommission_readiness(report)

    def test_skill_runtime_gate_error_codes_are_typed_quality_security_boundaries(self) -> None:
        self.assertEqual(error_policy("skill_runtime_artifact_untrusted")["category"], "security")
        for code in [
            "skill_runtime_benchmark_invalid",
            "skill_runtime_promotion_blocked",
            "skill_runtime_ops_readiness_blocked",
            "skill_runtime_decommission_blocked",
        ]:
            policy = error_policy(code)
            self.assertEqual(policy["category"], "quality_gate")
            self.assertIs(policy["retriable"], False)


def _valid_artifact_metadata() -> dict[str, str]:
    contract = load_skill_runtime_contract()
    return {
        "source": "ci_pipeline",
        "artifact_kind": "skill_policy_pyo3_wheel",
        "checksum_sha256": "sha256:skill-policy",
        "cargo_lock_digest": "cargo-lock:skill-runtime",
        "contract_version": contract["contract_version"],
        "bundle_revision": "skill-runtime-20260515.1",
        "schema_hash": contract["schema_hash"],
        "sbom_digest": "sbom:skill-runtime",
        "provenance_attestation": "slsa:intoto",
    }


def _valid_benchmark_report() -> dict[str, dict[str, dict[str, int | float]]]:
    contract = load_skill_runtime_contract()
    report: dict[str, dict[str, dict[str, int | float]]] = {}
    for baseline in contract["benchmark_policy"]["required_baselines"]:
        report[baseline] = {}
        for operation in contract["benchmark_policy"]["required_operations"]:
            report[baseline][operation] = {
                "p50_ms": 5,
                "p95_ms": 10,
                "p99_ms": 20,
                "queue_wait_ms": 1,
                "cpu_percent": 20.5,
                "memory_mb": 128,
            }
    return report


def _valid_promotion_report() -> dict[str, Any]:
    contract = load_skill_runtime_contract()
    return {
        "scope": "skill_sandbox",
        "shadow_days": 7,
        "shadow_samples": 1_000,
        "contract_mismatch_rate_ppm": 0,
        "panic_count": 0,
        "crash_count": 0,
        "python_legacy_p95_ms": 100,
        "rust_p95_ms": 110,
        "python_legacy_error_rate_ppm": 10,
        "rust_error_rate_ppm": 10,
        "evidence": {item: True for item in contract["promotion_policy"]["required_evidence"]},
    }


def _valid_ops_report() -> dict[str, dict[str, bool]]:
    contract = load_skill_runtime_contract()
    policy = contract["ops_policy"]
    return {
        "observability": {item: True for item in policy["required_observability"]},
        "runbooks": {item: True for item in policy["required_runbooks"]},
        "drills": {item: True for item in policy["required_drills"]},
    }


def _valid_decommission_report() -> dict[str, Any]:
    contract = load_skill_runtime_contract()
    policy = contract["decommission_policy"]
    return {
        "canonical_skill_runtime_stable": True,
        "rollback_path": "deployment_rollback",
        "legacy_paths_removed": {item: True for item in policy["required_removed_legacy_paths"]},
        "facade_only_paths": {item: True for item in policy["required_facade_only_paths"]},
        "evidence": {item: True for item in policy["required_evidence"]},
    }
