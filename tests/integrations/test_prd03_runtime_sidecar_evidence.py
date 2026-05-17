from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from src.storage.rust_contract import (
    artifact_policy,
    benchmark_policy,
    decommission_policy,
    load_runtime_sidecar_contract,
    migration_policy,
    ops_policy,
    promotion_policy,
)


SCRIPT = Path("scripts/validate_prd03_runtime_sidecar_evidence.py")


class Prd03RuntimeSidecarEvidenceTest(unittest.TestCase):
    def test_pending_evidence_ledger_is_allowed_only_for_non_release_ci(self) -> None:
        allowed = subprocess.run(
            [sys.executable, str(SCRIPT), "--allow-pending"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn("prd03_runtime_sidecar_evidence_pending", allowed.stdout)

        strict = subprocess.run(
            [sys.executable, str(SCRIPT)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(strict.returncode, 0)
        self.assertIn("prd03_runtime_sidecar_evidence_pending", strict.stderr)

    def test_complete_release_gate_evidence_passes_strict_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            evidence = Path(temp_dir) / "evidence.json"
            evidence.write_text(json.dumps(_valid_evidence()), encoding="utf-8")

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--evidence",
                    str(evidence),
                ],
                check=True,
                capture_output=True,
                text=True,
            )

        self.assertIn("prd03_runtime_sidecar_evidence_ready", result.stdout)


def _valid_evidence() -> dict[str, Any]:
    contract = load_runtime_sidecar_contract()
    artifact = artifact_policy()
    checksum = "sha256:runtime-sidecar"
    cargo_lock = "sha256:cargo-lock"
    benchmark = benchmark_policy()
    operation_metrics = {
        operation: {
            "p50_ms": 1.0,
            "p95_ms": 2.0,
            "p99_ms": 3.0,
            "queue_wait_ms": 0.5,
            "cpu_percent": 10.0,
            "memory_mb": 64.0,
            "throughput_per_sec": 100.0,
        }
        for operation in benchmark["required_operations"]
    }
    promotion = promotion_policy()
    migration = migration_policy()
    ops = ops_policy()
    decommission = decommission_policy()
    return {
        "schema_version": "maf.prd03.runtime_sidecar_evidence.v1",
        "status": "ready",
        "artifact_provenance": {
            "source": "ci_pipeline",
            "artifact_kind": "sidecar_binary",
            "checksum_sha256": checksum,
            "sbom_digest": "sha256:sbom",
            "cargo_lock_digest": cargo_lock,
            "proto_hash": artifact["expected_proto_hash"],
            "schema_hash": contract["schema_hash"],
            "provenance_attestation": "slsa-provenance",
        },
        "allowed_artifact_checksums": [checksum],
        "allowed_cargo_lock_digests": [cargo_lock],
        "benchmark_report": {
            "python_baseline": operation_metrics,
            "rust_sidecar_baseline": operation_metrics,
        },
        "promotion_readiness": {
            "scope": "single_instance",
            "shadow_days": promotion["min_shadow_days"],
            "shadow_samples": promotion["min_shadow_samples"],
            "contract_mismatch_rate_ppm": 0,
            "panic_count": 0,
            "crash_count": 0,
            "rust_p95_ms": 100.0,
            "python_legacy_p95_ms": 100.0,
            "rust_error_rate_ppm": 0,
            "python_legacy_error_rate_ppm": 0,
            "evidence": {item: True for item in promotion["required_evidence"]},
        },
        "migration_plan": {
            "target_schema_version": "runtime_store_schema_v2",
            "components": {
                component: {item: True for item in migration["required_evidence"]}
                for component in migration["required_components"]
            },
        },
        "ops_readiness": {
            "observability": {item: True for item in ops["required_observability"]},
            "runbooks": {item: True for item in ops["required_runbooks"]},
            "drills": {item: True for item in ops["required_drills"]},
        },
        "decommission_readiness": {
            "canonical_sidecar_stable": True,
            "rollback_path": "deployment_or_restore",
            "legacy_write_paths_removed": {
                item: True for item in decommission["required_removed_legacy_paths"]
            },
            "facade_only_paths": {item: True for item in decommission["required_facade_only_paths"]},
            "evidence": {item: True for item in decommission["required_evidence"]},
        },
        "blockers": [],
    }
