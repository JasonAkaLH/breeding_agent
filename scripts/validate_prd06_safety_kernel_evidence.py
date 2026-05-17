#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EVIDENCE = Path("docs/prd/rust/evidence/prd06/safety_kernel_release_gates.json")
CONTRACT_PATH = REPO_ROOT / "src" / "integrations" / "rust_contracts" / "safety_contract.json"
SCHEMA_VERSION = "maf.prd06.safety_kernel_evidence.v1"
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


class EvidenceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EvidenceError("prd06_safety_kernel_evidence_invalid", f"{path} is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise EvidenceError("prd06_safety_kernel_evidence_invalid", f"{path} must contain a JSON object")
    return payload


def _load_contract() -> dict[str, Any]:
    contract = _load_json(CONTRACT_PATH)
    if contract.get("component") != "maf_safety_kernels":
        raise EvidenceError("prd06_safety_kernel_evidence_invalid", "safety contract component mismatch")
    return contract


def _pending(value: Any) -> bool:
    return value is None or value == {} or (isinstance(value, Mapping) and value.get("status") == "pending")


def _validate_artifact(payload: Mapping[str, Any]) -> dict[str, str]:
    value = payload.get("artifact_provenance")
    if _pending(value):
        raise EvidenceError("prd06_safety_kernel_evidence_pending", "artifact provenance evidence is pending")
    if not isinstance(value, Mapping):
        raise EvidenceError("prd06_safety_kernel_evidence_invalid", "artifact_provenance must be a JSON object")
    if value.get("artifact_kind") != "safety_kernels_pyo3_wheel":
        raise EvidenceError(
            "prd06_safety_kernel_evidence_invalid",
            "artifact_provenance.artifact_kind must be safety_kernels_pyo3_wheel",
        )
    contract = _load_contract()
    for key in ("contract_version", "schema_hash", "error_code_table_hash"):
        if value.get(key) != contract.get(key):
            raise EvidenceError("prd06_safety_kernel_evidence_invalid", f"artifact_provenance.{key} mismatch")
    checksum = str(value.get("checksum_sha256") or "")
    cargo_lock_digest = str(value.get("cargo_lock_digest") or "")
    allowed_checksums = {str(item) for item in payload.get("allowed_artifact_checksums", [])}
    allowed_cargo_locks = {str(item) for item in payload.get("allowed_cargo_lock_digests", [])}
    if checksum not in allowed_checksums or cargo_lock_digest not in allowed_cargo_locks:
        raise EvidenceError("prd06_safety_kernel_evidence_invalid", "artifact provenance is not deployment-allowlisted")
    for key in ("source", "sbom_digest", "provenance_attestation"):
        if not value.get(key):
            raise EvidenceError("prd06_safety_kernel_evidence_invalid", f"artifact_provenance.{key} is required")
    return {str(key): str(val) for key, val in value.items()}


def _validate_fuzz_report(value: Mapping[str, Any]) -> dict[str, Any]:
    targets = value.get("targets")
    if not isinstance(targets, Mapping):
        raise EvidenceError("prd06_safety_kernel_evidence_invalid", "fuzz_report.targets must be a JSON object")
    missing = [target for target in FUZZ_TARGETS if targets.get(target) is not True]
    if missing:
        raise EvidenceError("prd06_safety_kernel_evidence_invalid", "missing passing fuzz targets: " + ",".join(missing))
    if int(value.get("bounded_smoke_seconds") or 0) < 30:
        raise EvidenceError("prd06_safety_kernel_evidence_invalid", "fuzz_report.bounded_smoke_seconds must be >= 30")
    if int(value.get("crash_count") or 0) != 0 or int(value.get("leak_count") or 0) != 0:
        raise EvidenceError("prd06_safety_kernel_evidence_invalid", "fuzz crash/leak count must be zero")
    return dict(value)


def _validate_coverage_report(value: Mapping[str, Any]) -> dict[str, Any]:
    coverage = value.get("line_coverage")
    if not isinstance(coverage, Mapping):
        raise EvidenceError("prd06_safety_kernel_evidence_invalid", "coverage_report.line_coverage must be a JSON object")
    low = [crate for crate in SECURITY_CRATES if float(coverage.get(crate) or 0) < 90]
    if low:
        raise EvidenceError("prd06_safety_kernel_evidence_invalid", "coverage below 90%: " + ",".join(low))
    return dict(value)


def _validate_benchmark_report(value: Mapping[str, Any]) -> dict[str, Any]:
    for baseline in ("python_legacy", "rust_kernel"):
        operations = value.get(baseline)
        if not isinstance(operations, Mapping):
            raise EvidenceError("prd06_safety_kernel_evidence_invalid", f"benchmark_report.{baseline} must be a JSON object")
        for operation in BENCHMARK_OPERATIONS:
            metrics = operations.get(operation)
            if not isinstance(metrics, Mapping):
                raise EvidenceError(
                    "prd06_safety_kernel_evidence_invalid",
                    f"benchmark_report.{baseline}.{operation} is required",
                )
            for metric in ("p50_ms", "p95_ms", "p99_ms", "cpu_percent", "memory_mb", "payload_bytes", "result_bytes"):
                if metric not in metrics:
                    raise EvidenceError(
                        "prd06_safety_kernel_evidence_invalid",
                        f"benchmark metric missing: {baseline}.{operation}.{metric}",
                    )
    return dict(value)


def _validate_promotion_readiness(value: Mapping[str, Any]) -> dict[str, Any]:
    if int(value.get("shadow_days") or 0) < 7:
        raise EvidenceError("prd06_safety_kernel_evidence_invalid", "promotion_readiness.shadow_days must be >= 7")
    for key in (
        "contract_mismatch_count",
        "secret_leak_count",
        "path_escape_count",
        "readonly_policy_bypass_count",
        "redaction_failure_count",
    ):
        if int(value.get(key) or 0) != 0:
            raise EvidenceError("prd06_safety_kernel_evidence_invalid", f"promotion_readiness.{key} must be zero")
    evidence = value.get("evidence")
    if not isinstance(evidence, Mapping) or not all(bool(item) for item in evidence.values()):
        raise EvidenceError("prd06_safety_kernel_evidence_invalid", "promotion_readiness evidence booleans must all pass")
    return dict(value)


def _validate_ops_readiness(value: Mapping[str, Any]) -> dict[str, Any]:
    for section in ("observability", "alerts", "drills"):
        mapping = value.get(section)
        if not isinstance(mapping, Mapping) or not mapping or not all(bool(item) for item in mapping.values()):
            raise EvidenceError("prd06_safety_kernel_evidence_invalid", f"ops_readiness.{section} booleans must all pass")
    return dict(value)


def _validate_decommission_readiness(value: Mapping[str, Any]) -> dict[str, Any]:
    if value.get("canonical_safety_kernels_stable") is not True:
        raise EvidenceError("prd06_safety_kernel_evidence_invalid", "canonical safety kernels must be stable")
    for section in ("legacy_paths_removed", "facade_only_paths", "evidence"):
        mapping = value.get(section)
        if not isinstance(mapping, Mapping) or not mapping or not all(bool(item) for item in mapping.values()):
            raise EvidenceError("prd06_safety_kernel_evidence_invalid", f"decommission_readiness.{section} booleans must all pass")
    if not value.get("rollback_path"):
        raise EvidenceError("prd06_safety_kernel_evidence_invalid", "decommission_readiness.rollback_path is required")
    return dict(value)


def _validate_required_mapping(
    payload: Mapping[str, Any],
    key: str,
    validator: Callable[[Mapping[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    value = payload.get(key)
    if _pending(value):
        raise EvidenceError("prd06_safety_kernel_evidence_pending", f"{key} evidence is pending")
    if not isinstance(value, Mapping):
        raise EvidenceError("prd06_safety_kernel_evidence_invalid", f"{key} must be a JSON object")
    return validator(value)


def validate_evidence(payload: Mapping[str, Any], *, allow_pending: bool) -> dict[str, Any]:
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise EvidenceError("prd06_safety_kernel_evidence_invalid", "unsupported evidence schema_version")

    gate_specs: tuple[tuple[str, Callable[[], dict[str, Any]]], ...] = (
        ("artifact_provenance", lambda: _validate_artifact(payload)),
        ("fuzz_report", lambda: _validate_required_mapping(payload, "fuzz_report", _validate_fuzz_report)),
        ("coverage_report", lambda: _validate_required_mapping(payload, "coverage_report", _validate_coverage_report)),
        ("benchmark_report", lambda: _validate_required_mapping(payload, "benchmark_report", _validate_benchmark_report)),
        (
            "promotion_readiness",
            lambda: _validate_required_mapping(payload, "promotion_readiness", _validate_promotion_readiness),
        ),
        ("ops_readiness", lambda: _validate_required_mapping(payload, "ops_readiness", _validate_ops_readiness)),
        (
            "decommission_readiness",
            lambda: _validate_required_mapping(payload, "decommission_readiness", _validate_decommission_readiness),
        ),
    )

    results: dict[str, Any] = {}
    pending: list[str] = []
    for gate, check in gate_specs:
        try:
            results[gate] = check()
        except EvidenceError as exc:
            if exc.code == "prd06_safety_kernel_evidence_pending" and allow_pending:
                pending.append(gate)
                results[gate] = {"status": "pending", "reason": str(exc)}
                continue
            raise

    blockers = payload.get("blockers", [])
    if blockers and not allow_pending:
        raise EvidenceError(
            "prd06_safety_kernel_evidence_pending",
            "external blockers remain: " + ", ".join(str(item) for item in blockers),
        )
    return {
        "status": "ready" if not pending and not blockers else "pending",
        "pending_gates": pending,
        "blockers": blockers,
        "results": results,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate PRD06 Artifact/Auth/DataAccess/Audit safety-kernel release evidence.")
    parser.add_argument("--evidence", type=Path, default=DEFAULT_EVIDENCE)
    parser.add_argument(
        "--allow-pending",
        action="store_true",
        help="Treat missing external production evidence as a non-release-blocking pending status.",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable validation result.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if not args.evidence.exists():
            if args.allow_pending:
                result = {
                    "status": "pending",
                    "pending_gates": ["evidence_file"],
                    "blockers": [f"{args.evidence} does not exist"],
                    "results": {},
                }
            else:
                raise EvidenceError("prd06_safety_kernel_evidence_pending", f"{args.evidence} does not exist")
        else:
            result = validate_evidence(_load_json(args.evidence), allow_pending=args.allow_pending)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        elif result["status"] == "ready":
            print("prd06_safety_kernel_evidence_ready")
        else:
            print("prd06_safety_kernel_evidence_pending: " + ",".join(result["pending_gates"]))
        return 0 if result["status"] == "ready" or args.allow_pending else 1
    except EvidenceError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
