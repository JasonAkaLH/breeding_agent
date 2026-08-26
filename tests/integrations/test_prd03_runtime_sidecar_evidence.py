from __future__ import annotations

import json
import hashlib
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
            "target_schema_version": contract["schema_hash"],
            "components": {
                component: {item: True for item in migration["required_evidence"]}
                for component in migration["required_components"]
            },
            "task_authority_cutover": _valid_task_authority_cutover(),
            "submission_authority_cutover": _valid_submission_authority_cutover(
                contract
            ),
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


def _valid_task_authority_cutover() -> dict[str, Any]:
    digest = "a" * 64
    return {
        "backfill_import_complete": True,
        "task_inventory": {
            "legacy_count": 1,
            "sidecar_count": 1,
            "legacy_canonical_digest": digest,
            "sidecar_canonical_digest": digest,
        },
        "task_node_inventory": {
            "legacy_count": 1,
            "sidecar_count": 1,
            "legacy_canonical_digest": digest,
            "sidecar_canonical_digest": digest,
        },
        "legacy_null_assignment_resolution": {
            "resolution_complete": True,
            "active_count": 0,
            "active_canonical_digest": hashlib.sha256(b"[]").hexdigest(),
            "terminal_historical_count": 1,
            "terminal_historical_canonical_digest": digest,
            "terminal_historical_remains_unassigned": True,
        },
    }


def _valid_submission_authority_cutover(
    contract: dict[str, Any],
) -> dict[str, Any]:
    empty_inventory = {
        "count": 0,
        "pk_sha256": "b" * 64,
        "canonical_sha256": "c" * 64,
        "finalize_empty": True,
    }
    matching_inventory = {
        "source": empty_inventory,
        "destination": empty_inventory,
        "ambiguity_count": 0,
    }
    return {
        "source_backend": "sqlite",
        "source_identity_sha256": "d" * 64,
        "snapshot_boundary_sha256": "e" * 64,
        "writer_fence_sha256": "f" * 64,
        "report_sha256": "1" * 64,
        "tested_commit": "2" * 40,
        "tested_tree": "3" * 40,
        "destination_contract": {
            "schema_hash": contract["schema_hash"],
            "proto_hash": contract["artifact_policy"]["expected_proto_hash"],
            "error_code_table_hash": contract["error_code_table_hash"],
            "supported_features_sha256": hashlib.sha256(
                json.dumps(
                    contract["supported_features"],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
        },
        "conversation_inventory": matching_inventory,
        "message_identity_inventory": matching_inventory,
        "active_task_inventory": matching_inventory,
        "finalization_receipt_sha256": "4" * 64,
        "finalized_at_ms": 1,
    }
