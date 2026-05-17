from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from scripts.validate_prd04_skill_runtime_evidence import validate_evidence
from src.integrations.codex_skills.rust_contract import load_skill_runtime_contract


class PRD04SkillRuntimeEvidenceTest(unittest.TestCase):
    def test_pending_ledger_is_allowed_only_with_allow_pending(self) -> None:
        evidence_path = Path("docs/prd/rust/evidence/prd04/skill_runtime_release_gates.json")

        allowed = subprocess.run(
            [
                sys.executable,
                "scripts/validate_prd04_skill_runtime_evidence.py",
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
            [sys.executable, "scripts/validate_prd04_skill_runtime_evidence.py", "--evidence", str(evidence_path)],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        allowed_payload = json.loads(allowed.stdout)
        self.assertEqual(allowed_payload["status"], "pending")
        self.assertIn("artifact_provenance", allowed_payload["pending_gates"])
        self.assertNotEqual(strict.returncode, 0)
        self.assertIn("prd04_skill_runtime_evidence_pending", strict.stderr)

    def test_complete_synthetic_evidence_validates_all_prd04_gates(self) -> None:
        contract = load_skill_runtime_contract()
        payload = {
            "schema_version": "maf.prd04.skill_runtime_evidence.v1",
            "status": "ready",
            "last_updated": "2026-05-17",
            "artifact_provenance": {
                "skill_policy_wheel": _artifact("skill_policy_pyo3_wheel", "sha256:policy"),
                "skill_sandbox_sidecar": _artifact("skill_sandbox_sidecar_binary", "sha256:sandbox"),
            },
            "allowed_artifact_checksums": ["sha256:policy", "sha256:sandbox"],
            "allowed_cargo_lock_digests": ["sha256:cargo-lock"],
            "benchmark_report": _benchmark(contract),
            "promotion_readiness": _promotion(contract),
            "ops_readiness": _ops(contract),
            "decommission_readiness": _decommission(contract),
            "blockers": [],
        }

        result = validate_evidence(payload, allow_pending=False)

        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["pending_gates"], [])
        self.assertEqual(
            sorted(result["results"]["artifact_provenance"]),
            ["skill_policy_wheel", "skill_sandbox_sidecar"],
        )


def _artifact(kind: str, checksum: str) -> dict[str, str]:
    contract = load_skill_runtime_contract()
    return {
        "source": "ci_pipeline",
        "artifact_kind": kind,
        "checksum_sha256": checksum,
        "cargo_lock_digest": "sha256:cargo-lock",
        "contract_version": contract["contract_version"],
        "bundle_revision": "skill-runtime-20260517.1",
        "schema_hash": contract["schema_hash"],
        "sbom_digest": "sha256:sbom",
        "provenance_attestation": "sha256:provenance",
    }


def _benchmark(contract: dict[str, Any]) -> dict[str, Any]:
    return {
        baseline: {
            operation: {
                "p50_ms": 1,
                "p95_ms": 2,
                "p99_ms": 3,
                "queue_wait_ms": 0,
                "cpu_percent": 10,
                "memory_mb": 64,
            }
            for operation in contract["benchmark_policy"]["required_operations"]
        }
        for baseline in contract["benchmark_policy"]["required_baselines"]
    }


def _promotion(contract: dict[str, Any]) -> dict[str, Any]:
    return {
        "scope": "skill_sandbox",
        "shadow_days": 7,
        "shadow_samples": 1000,
        "contract_mismatch_rate_ppm": 0,
        "panic_count": 0,
        "crash_count": 0,
        "python_legacy_p95_ms": 100,
        "rust_p95_ms": 100,
        "python_legacy_error_rate_ppm": 1,
        "rust_error_rate_ppm": 1,
        "evidence": {item: True for item in contract["promotion_policy"]["required_evidence"]},
    }


def _ops(contract: dict[str, Any]) -> dict[str, Any]:
    policy = contract["ops_policy"]
    return {
        "observability": {item: True for item in policy["required_observability"]},
        "runbooks": {item: True for item in policy["required_runbooks"]},
        "drills": {item: True for item in policy["required_drills"]},
    }


def _decommission(contract: dict[str, Any]) -> dict[str, Any]:
    policy = contract["decommission_policy"]
    return {
        "canonical_skill_runtime_stable": True,
        "rollback_path": "deployment_rollback",
        "legacy_paths_removed": {item: True for item in policy["required_removed_legacy_paths"]},
        "facade_only_paths": {item: True for item in policy["required_facade_only_paths"]},
        "evidence": {item: True for item in policy["required_evidence"]},
    }


if __name__ == "__main__":
    unittest.main()
