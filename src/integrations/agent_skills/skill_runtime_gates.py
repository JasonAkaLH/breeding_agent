from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from src.core.evidence import (
    is_number as _is_number,
    non_negative_number as _non_negative_number,
    number_at_least as _number_at_least,
    number_at_most as _number_at_most,
    require_boolean_evidence as _require_boolean_evidence,
)

from .rust_contract import (
    artifact_policy,
    benchmark_policy,
    decommission_policy,
    error_policy,
    load_skill_runtime_contract,
    ops_policy,
    promotion_policy,
)


def validate_skill_runtime_artifact_provenance(
    metadata: Mapping[str, Any],
    *,
    allowed_checksums: set[str] | frozenset[str] | tuple[str, ...],
    allowed_cargo_lock_digests: set[str] | frozenset[str] | tuple[str, ...],
) -> dict[str, str]:
    """Validate prebuilt Skill Runtime artifact provenance before production use."""

    policy = artifact_policy()
    if not isinstance(metadata, Mapping):
        _raise_artifact_untrusted()
    required_fields = {str(field) for field in policy["required_fields"]}
    if any(not _non_empty_string(metadata.get(field)) for field in required_fields):
        _raise_artifact_untrusted()

    source = str(metadata["source"]).strip().lower()
    if source not in set(policy["allowed_sources"]):
        _raise_artifact_untrusted()
    artifact_kind = str(metadata["artifact_kind"])
    if artifact_kind not in set(policy["allowed_artifact_kinds"]):
        _raise_artifact_untrusted()
    checksum = str(metadata["checksum_sha256"])
    cargo_lock_digest = str(metadata["cargo_lock_digest"])
    if policy.get("require_checksum_allowlist") is True and checksum not in set(allowed_checksums):
        _raise_artifact_untrusted()
    if (
        policy.get("require_cargo_lock_digest_allowlist") is True
        and cargo_lock_digest not in set(allowed_cargo_lock_digests)
    ):
        _raise_artifact_untrusted()

    contract = load_skill_runtime_contract()
    if policy.get("require_contract_version_match") is True and str(metadata["contract_version"]) != contract["contract_version"]:
        _raise_artifact_untrusted()
    if policy.get("require_schema_hash_match") is True and str(metadata["schema_hash"]) != contract["schema_hash"]:
        _raise_artifact_untrusted()

    return {
        "source": source,
        "artifact_kind": artifact_kind,
        "checksum_sha256": checksum,
        "cargo_lock_digest": cargo_lock_digest,
        "contract_version": str(metadata["contract_version"]),
        "bundle_revision": str(metadata["bundle_revision"]),
        "schema_hash": str(metadata["schema_hash"]),
        "provenance_attestation": "configured",
        "sbom": "configured",
    }


def validate_skill_runtime_benchmark_report(report: Mapping[str, Any]) -> dict[str, str]:
    """Validate benchmark evidence required before Skill Runtime promotion."""

    policy = benchmark_policy()
    if not isinstance(report, Mapping):
        _raise_benchmark_invalid()
    required_baselines = [str(baseline) for baseline in policy["required_baselines"]]
    required_operations = [str(operation) for operation in policy["required_operations"]]
    required_metrics = [str(metric) for metric in policy["required_metrics"]]
    for baseline in required_baselines:
        baseline_report = report.get(baseline)
        if not isinstance(baseline_report, Mapping):
            _raise_benchmark_invalid()
        for operation in required_operations:
            metrics = baseline_report.get(operation)
            if not isinstance(metrics, Mapping):
                _raise_benchmark_invalid()
            for metric in required_metrics:
                value = metrics.get(metric)
                if not _non_negative_number(value):
                    _raise_benchmark_invalid()
    return {
        "baselines": ",".join(required_baselines),
        "operations": ",".join(required_operations),
        "metrics": ",".join(required_metrics),
    }


def validate_skill_runtime_promotion_readiness(report: Mapping[str, Any]) -> dict[str, str]:
    """Validate shadow-to-enforce thresholds for Skill Runtime production promotion."""

    policy = promotion_policy()
    if not isinstance(report, Mapping):
        _raise_promotion_blocked()
    if report.get("scope") not in set(policy["allowed_scopes"]):
        _raise_promotion_blocked()
    if not _number_at_least(report.get("shadow_days"), policy["min_shadow_days"]):
        _raise_promotion_blocked()
    if not _number_at_least(report.get("shadow_samples"), policy["min_shadow_samples"]):
        _raise_promotion_blocked()
    if not _number_at_most(report.get("contract_mismatch_rate_ppm"), policy["max_contract_mismatch_rate_ppm"]):
        _raise_promotion_blocked()
    if not _number_at_most(report.get("panic_count"), policy["max_panic_count"]):
        _raise_promotion_blocked()
    if not _number_at_most(report.get("crash_count"), policy["max_crash_count"]):
        _raise_promotion_blocked()
    python_p95_ms = report.get("python_legacy_p95_ms")
    rust_p95_ms = report.get("rust_p95_ms")
    if not (
        _is_number(python_p95_ms)
        and python_p95_ms > 0
        and _is_number(rust_p95_ms)
        and rust_p95_ms <= python_p95_ms * (policy["max_p95_latency_ratio_percent"] / 100)
    ):
        _raise_promotion_blocked()
    if policy.get("error_rate_must_not_exceed_legacy") is True and not _number_at_most(
        report.get("rust_error_rate_ppm"),
        report.get("python_legacy_error_rate_ppm"),
    ):
        _raise_promotion_blocked()
    _require_boolean_evidence(report.get("evidence"), [str(item) for item in policy["required_evidence"]], _raise_promotion_blocked)
    return {
        "promotion": "ready",
        "scope": str(report["scope"]),
        "shadow_days": str(report["shadow_days"]),
        "shadow_samples": str(report["shadow_samples"]),
    }


def validate_skill_runtime_ops_readiness(report: Mapping[str, Any]) -> dict[str, str]:
    """Validate Skill Runtime observability, runbook, and fault-drill evidence."""

    policy = ops_policy()
    if not isinstance(report, Mapping):
        _raise_ops_readiness_blocked()
    required_observability = [str(item) for item in policy["required_observability"]]
    required_runbooks = [str(item) for item in policy["required_runbooks"]]
    required_drills = [str(item) for item in policy["required_drills"]]
    _require_boolean_evidence(report.get("observability"), required_observability, _raise_ops_readiness_blocked)
    _require_boolean_evidence(report.get("runbooks"), required_runbooks, _raise_ops_readiness_blocked)
    _require_boolean_evidence(report.get("drills"), required_drills, _raise_ops_readiness_blocked)
    return {
        "ops": "ready",
        "observability": ",".join(required_observability),
        "runbooks": ",".join(required_runbooks),
        "drills": ",".join(required_drills),
    }


def validate_skill_runtime_decommission_readiness(report: Mapping[str, Any]) -> dict[str, str]:
    """Validate evidence before removing legacy Python Skill Runtime policy paths."""

    policy = decommission_policy()
    if not isinstance(report, Mapping):
        _raise_decommission_blocked()
    if report.get("canonical_skill_runtime_stable") is not True:
        _raise_decommission_blocked()
    rollback_path = report.get("rollback_path")
    if rollback_path not in set(policy["allowed_rollback_paths"]):
        _raise_decommission_blocked()
    required_removed_legacy_paths = [str(item) for item in policy["required_removed_legacy_paths"]]
    required_facade_only_paths = [str(item) for item in policy["required_facade_only_paths"]]
    required_evidence = [str(item) for item in policy["required_evidence"]]
    _require_boolean_evidence(
        report.get("legacy_paths_removed"),
        required_removed_legacy_paths,
        _raise_decommission_blocked,
    )
    _require_boolean_evidence(report.get("facade_only_paths"), required_facade_only_paths, _raise_decommission_blocked)
    _require_boolean_evidence(report.get("evidence"), required_evidence, _raise_decommission_blocked)
    return {
        "decommission": "ready",
        "rollback_path": str(rollback_path),
        "removed_legacy_paths": ",".join(required_removed_legacy_paths),
        "facade_only_paths": ",".join(required_facade_only_paths),
        "evidence": ",".join(required_evidence),
    }


def _non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _raise_artifact_untrusted() -> None:
    code = error_policy("skill_runtime_artifact_untrusted")["code"]
    raise RuntimeError(f"{code}: Rust Skill Runtime artifact provenance is not trusted")


def _raise_benchmark_invalid() -> None:
    code = error_policy("skill_runtime_benchmark_invalid")["code"]
    raise RuntimeError(f"{code}: Rust Skill Runtime benchmark report is incomplete")


def _raise_promotion_blocked() -> None:
    code = error_policy("skill_runtime_promotion_blocked")["code"]
    raise RuntimeError(f"{code}: Rust Skill Runtime promotion threshold is not satisfied")


def _raise_ops_readiness_blocked() -> None:
    code = error_policy("skill_runtime_ops_readiness_blocked")["code"]
    raise RuntimeError(f"{code}: Rust Skill Runtime ops readiness evidence is incomplete")


def _raise_decommission_blocked() -> None:
    code = error_policy("skill_runtime_decommission_blocked")["code"]
    raise RuntimeError(f"{code}: Rust Skill Runtime legacy decommission evidence is incomplete")


__all__ = [
    "validate_skill_runtime_artifact_provenance",
    "validate_skill_runtime_benchmark_report",
    "validate_skill_runtime_decommission_readiness",
    "validate_skill_runtime_ops_readiness",
    "validate_skill_runtime_promotion_readiness",
]
