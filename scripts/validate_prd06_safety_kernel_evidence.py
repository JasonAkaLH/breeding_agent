#!/usr/bin/env python3
from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.prd_evidence import (
    EvidenceError,
    GateSpec,
    allowed_digest_sets,
    collect_gate_results,
    finish_release_gate_result,
    is_pending,
    load_json_object,
    required_mapping,
    run_evidence_cli,
    validate_schema_version,
)

DEFAULT_EVIDENCE = Path("docs/prd/rust/evidence/prd06/safety_kernel_release_gates.json")
CONTRACT_PATH = REPO_ROOT / "src" / "integrations" / "rust_contracts" / "safety_contract.json"
SCHEMA_VERSION = "maf.prd06.safety_kernel_evidence.v1"
INVALID_CODE = "prd06_safety_kernel_evidence_invalid"
PENDING_CODE = "prd06_safety_kernel_evidence_pending"
SECURITY_CRATES = (
    "maf_artifact_store",
    "maf_auth_core",
    "maf_data_access",
    "maf_audit_sanitizer",
)
FUZZ_TARGETS = (
    "artifact_path",
    "auth_core",
    "audit_sanitizer",
    "data_access_readonly",
)
BENCHMARK_OPERATIONS = (
    "artifact_path_normalization",
    "archive_safety",
    "artifact_hash",
    "auth_primitive",
    "readonly_row_shaping",
    "audit_redaction",
)


def _load_contract() -> dict[str, Any]:
    contract = load_json_object(CONTRACT_PATH, invalid_code=INVALID_CODE)
    if contract.get("component") != "maf_safety_kernels":
        raise EvidenceError(INVALID_CODE, "safety contract component mismatch")
    return contract


def _validate_artifact(payload: Mapping[str, Any]) -> dict[str, str]:
    value = payload.get("artifact_provenance")
    if is_pending(value):
        raise EvidenceError(PENDING_CODE, "artifact provenance evidence is pending")
    if not isinstance(value, Mapping):
        raise EvidenceError(INVALID_CODE, "artifact_provenance must be a JSON object")
    if value.get("artifact_kind") != "safety_kernels_pyo3_wheel":
        raise EvidenceError(
            INVALID_CODE,
            "artifact_provenance.artifact_kind must be safety_kernels_pyo3_wheel",
        )
    contract = _load_contract()
    for key in ("contract_version", "schema_hash", "error_code_table_hash"):
        if value.get(key) != contract.get(key):
            raise EvidenceError(INVALID_CODE, f"artifact_provenance.{key} mismatch")
    checksum = str(value.get("checksum_sha256") or "")
    cargo_lock_digest = str(value.get("cargo_lock_digest") or "")
    allowed_checksums, allowed_cargo_locks = allowed_digest_sets(payload, invalid_code=INVALID_CODE)
    if checksum not in allowed_checksums or cargo_lock_digest not in allowed_cargo_locks:
        raise EvidenceError(INVALID_CODE, "artifact provenance is not deployment-allowlisted")
    for key in ("source", "sbom_digest", "provenance_attestation"):
        if not value.get(key):
            raise EvidenceError(INVALID_CODE, f"artifact_provenance.{key} is required")
    return {str(key): str(val) for key, val in value.items()}


def _validate_fuzz_report(value: Mapping[str, Any]) -> dict[str, Any]:
    targets = value.get("targets")
    if not isinstance(targets, Mapping):
        raise EvidenceError(INVALID_CODE, "fuzz_report.targets must be a JSON object")
    missing = [target for target in FUZZ_TARGETS if targets.get(target) is not True]
    if missing:
        raise EvidenceError(INVALID_CODE, "missing passing fuzz targets: " + ",".join(missing))
    if int(value.get("bounded_smoke_seconds") or 0) < 30:
        raise EvidenceError(INVALID_CODE, "fuzz_report.bounded_smoke_seconds must be >= 30")
    if int(value.get("crash_count") or 0) != 0 or int(value.get("leak_count") or 0) != 0:
        raise EvidenceError(INVALID_CODE, "fuzz crash/leak count must be zero")
    return dict(value)


def _validate_coverage_report(value: Mapping[str, Any]) -> dict[str, Any]:
    coverage = value.get("line_coverage")
    if not isinstance(coverage, Mapping):
        raise EvidenceError(INVALID_CODE, "coverage_report.line_coverage must be a JSON object")
    low = [crate for crate in SECURITY_CRATES if float(coverage.get(crate) or 0) < 90]
    if low:
        raise EvidenceError(INVALID_CODE, "coverage below 90%: " + ",".join(low))
    return dict(value)


def _validate_benchmark_report(value: Mapping[str, Any]) -> dict[str, Any]:
    for baseline in ("python_legacy", "rust_kernel"):
        operations = value.get(baseline)
        if not isinstance(operations, Mapping):
            raise EvidenceError(INVALID_CODE, f"benchmark_report.{baseline} must be a JSON object")
        for operation in BENCHMARK_OPERATIONS:
            metrics = operations.get(operation)
            if not isinstance(metrics, Mapping):
                raise EvidenceError(INVALID_CODE, f"benchmark_report.{baseline}.{operation} is required")
            for metric in ("p50_ms", "p95_ms", "p99_ms", "cpu_percent", "memory_mb", "payload_bytes", "result_bytes"):
                if metric not in metrics:
                    raise EvidenceError(INVALID_CODE, f"benchmark metric missing: {baseline}.{operation}.{metric}")
    return dict(value)


def _validate_promotion_readiness(value: Mapping[str, Any]) -> dict[str, Any]:
    if int(value.get("shadow_days") or 0) < 7:
        raise EvidenceError(INVALID_CODE, "promotion_readiness.shadow_days must be >= 7")
    for key in (
        "contract_mismatch_count",
        "secret_leak_count",
        "path_escape_count",
        "readonly_policy_bypass_count",
        "redaction_failure_count",
    ):
        if int(value.get(key) or 0) != 0:
            raise EvidenceError(INVALID_CODE, f"promotion_readiness.{key} must be zero")
    evidence = value.get("evidence")
    if not isinstance(evidence, Mapping) or not all(bool(item) for item in evidence.values()):
        raise EvidenceError(INVALID_CODE, "promotion_readiness evidence booleans must all pass")
    return dict(value)


def _validate_ops_readiness(value: Mapping[str, Any]) -> dict[str, Any]:
    for section in ("observability", "alerts", "drills"):
        mapping = value.get(section)
        if not isinstance(mapping, Mapping) or not mapping or not all(bool(item) for item in mapping.values()):
            raise EvidenceError(INVALID_CODE, f"ops_readiness.{section} booleans must all pass")
    return dict(value)


def _validate_decommission_readiness(value: Mapping[str, Any]) -> dict[str, Any]:
    if value.get("canonical_safety_kernels_stable") is not True:
        raise EvidenceError(INVALID_CODE, "canonical safety kernels must be stable")
    for section in ("legacy_paths_removed", "facade_only_paths", "evidence"):
        mapping = value.get(section)
        if not isinstance(mapping, Mapping) or not mapping or not all(bool(item) for item in mapping.values()):
            raise EvidenceError(INVALID_CODE, f"decommission_readiness.{section} booleans must all pass")
    if not value.get("rollback_path"):
        raise EvidenceError(INVALID_CODE, "decommission_readiness.rollback_path is required")
    return dict(value)


def validate_evidence(payload: Mapping[str, Any], *, allow_pending: bool) -> dict[str, Any]:
    validate_schema_version(payload, expected=SCHEMA_VERSION, invalid_code=INVALID_CODE)

    gate_specs: tuple[GateSpec, ...] = (
        ("artifact_provenance", lambda: _validate_artifact(payload)),
        (
            "fuzz_report",
            lambda: required_mapping(
                payload,
                "fuzz_report",
                _validate_fuzz_report,
                pending_code=PENDING_CODE,
                invalid_code=INVALID_CODE,
            ),
        ),
        (
            "coverage_report",
            lambda: required_mapping(
                payload,
                "coverage_report",
                _validate_coverage_report,
                pending_code=PENDING_CODE,
                invalid_code=INVALID_CODE,
            ),
        ),
        (
            "benchmark_report",
            lambda: required_mapping(
                payload,
                "benchmark_report",
                _validate_benchmark_report,
                pending_code=PENDING_CODE,
                invalid_code=INVALID_CODE,
            ),
        ),
        (
            "promotion_readiness",
            lambda: required_mapping(
                payload,
                "promotion_readiness",
                _validate_promotion_readiness,
                pending_code=PENDING_CODE,
                invalid_code=INVALID_CODE,
            ),
        ),
        (
            "ops_readiness",
            lambda: required_mapping(
                payload,
                "ops_readiness",
                _validate_ops_readiness,
                pending_code=PENDING_CODE,
                invalid_code=INVALID_CODE,
            ),
        ),
        (
            "decommission_readiness",
            lambda: required_mapping(
                payload,
                "decommission_readiness",
                _validate_decommission_readiness,
                pending_code=PENDING_CODE,
                invalid_code=INVALID_CODE,
            ),
        ),
    )
    results, pending = collect_gate_results(gate_specs, allow_pending=allow_pending, pending_code=PENDING_CODE)
    return finish_release_gate_result(
        payload,
        results=results,
        pending=pending,
        allow_pending=allow_pending,
        pending_code=PENDING_CODE,
    )


def main() -> int:
    return run_evidence_cli(
        validate_evidence,
        default_evidence=DEFAULT_EVIDENCE,
        description="Validate PRD06 Artifact/Auth/DataAccess/Audit safety-kernel release evidence.",
        invalid_code=INVALID_CODE,
        missing_pending_code=PENDING_CODE,
        status_messages={"ready": "prd06_safety_kernel_evidence_ready"},
        pending_message_prefix="prd06_safety_kernel_evidence_pending",
        catch_runtime_error=False,
    )


if __name__ == "__main__":
    raise SystemExit(main())
