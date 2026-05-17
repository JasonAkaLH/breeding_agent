from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .rust_contract import load_mcp_runtime_contract

_ALLOWED_SOURCES = frozenset({"ci_pipeline", "deployment_pipeline", "runtime_allowlist"})
_ALLOWED_ARTIFACT_KINDS = frozenset({"mcp_runtime_sidecar_binary"})
_ARTIFACT_MANIFEST_SCHEMA_VERSION = "maf.rust_artifact_provenance.v1"
_ARTIFACT_ALLOWLIST_SCHEMA_VERSION = "maf.rust_artifact_allowlist.v1"
_MCP_RUNTIME_COMPONENT = "maf_mcp_runtime"
_MCP_RUNTIME_ARTIFACT_ID = "maf_mcp_runtime_sidecar"
_MCP_RUNTIME_PROTO_HASH = "maf_mcp_proto_v1_20260517"
_GENERIC_ARTIFACT_KIND_MAP = {
    ("maf_mcp_runtime_sidecar", "sidecar_binary"): "mcp_runtime_sidecar_binary",
}
_EXACT_ALLOWLIST_FIELDS = (
    "component",
    "artifact_id",
    "artifact_kind",
    "artifact_sha256",
    "cargo_lock_sha256",
    "sbom_sha256",
    "provenance_sha256",
    "source",
    "git_commit",
    "toolchain",
    "target_triple",
    "build_profile",
)
_NESTED_ALLOWLIST_FIELDS = ("cargo_features", "contract_hashes", "proto_hashes")
_BENCHMARK_BASELINES = ("python_legacy", "rust_sidecar")
_BENCHMARK_OPERATIONS = (
    "initialize",
    "list_tools",
    "call_tool",
    "sse_stream",
    "task_result",
    "output_sanitizer",
    "bundle_activation",
)
_BENCHMARK_METRICS = (
    "p50_ms",
    "p95_ms",
    "p99_ms",
    "cpu_percent",
    "memory_mb",
    "raw_output_bytes",
    "sanitized_output_bytes",
)
_CONFORMANCE_PHASES = ("phase_0", "phase_1", "phase_2", "phase_3", "phase_4", "phase_5")
_OPS_OBSERVABILITY = ("health_dashboard", "readiness_dashboard", "slo_dashboard", "structured_metrics")
_OPS_ALERTS = (
    "sidecar_unavailable",
    "external_server_unavailable",
    "stream_idle_or_reconnect",
    "registry_write_failure",
    "sanitizer_or_redaction_failure",
    "bundle_quarantine",
)
_OPS_DRILLS = ("drain_restart", "registry_restore", "bundle_rollback", "identity_failure")
_RECOVERY_EVIDENCE = ("migration_lock", "backup", "restore", "replay_check", "rollback", "roll_forward")
_LEGACY_REMOVED = (
    "python_jsonrpc_canonical_parser",
    "python_sse_canonical_router",
    "python_output_sanitizer_canonical_logic",
    "python_bundle_activation_canonical_logic",
    "python_long_task_registry_production_path",
)
_FACADE_ONLY = ("mcp_executor_facade", "mcp_sidecar_client", "capability_descriptor_sync")
_DECOMMISSION_EVIDENCE = ("architecture_guard", "rollback_path", "owner_signoff")


def validate_mcp_runtime_artifact_provenance(
    metadata: Mapping[str, Any],
    *,
    allowed_checksums: set[str] | frozenset[str] | tuple[str, ...],
    allowed_cargo_lock_digests: set[str] | frozenset[str] | tuple[str, ...],
) -> dict[str, str]:
    if not isinstance(metadata, Mapping):
        _raise_artifact_untrusted()
    required = (
        "source",
        "artifact_kind",
        "checksum_sha256",
        "cargo_lock_digest",
        "protocol_version",
        "schema_hash",
        "error_code_table_hash",
        "proto_hash",
        "sbom_digest",
        "provenance_attestation",
    )
    if any(not _non_empty_string(metadata.get(field)) for field in required):
        _raise_artifact_untrusted()
    source = str(metadata["source"]).strip().lower()
    artifact_kind = str(metadata["artifact_kind"])
    checksum = str(metadata["checksum_sha256"])
    cargo_lock_digest = str(metadata["cargo_lock_digest"])
    contract = load_mcp_runtime_contract()
    if source not in _ALLOWED_SOURCES:
        _raise_artifact_untrusted()
    if artifact_kind not in _ALLOWED_ARTIFACT_KINDS:
        _raise_artifact_untrusted()
    if checksum not in set(allowed_checksums):
        _raise_artifact_untrusted()
    if cargo_lock_digest not in set(allowed_cargo_lock_digests):
        _raise_artifact_untrusted()
    if str(metadata["protocol_version"]) != contract["protocol_version"]:
        _raise_artifact_untrusted()
    if str(metadata["schema_hash"]) != contract["schema_hash"]:
        _raise_artifact_untrusted()
    if str(metadata["error_code_table_hash"]) != contract["error_code_table_hash"]:
        _raise_artifact_untrusted()
    if str(metadata["proto_hash"]) != _MCP_RUNTIME_PROTO_HASH:
        _raise_artifact_untrusted()
    return {
        "source": source,
        "artifact_kind": artifact_kind,
        "checksum_sha256": checksum,
        "cargo_lock_digest": cargo_lock_digest,
        "protocol_version": str(metadata["protocol_version"]),
        "schema_hash": str(metadata["schema_hash"]),
        "error_code_table_hash": str(metadata["error_code_table_hash"]),
        "proto_hash": str(metadata["proto_hash"]),
        "provenance_attestation": "configured",
        "sbom": "configured",
    }


def load_mcp_runtime_artifact_trust(
    *,
    manifest_path: str,
    allowlist_path: str,
) -> tuple[dict[str, Any], tuple[str, ...], tuple[str, ...]]:
    manifest = _load_json_object(Path(manifest_path), "MCP Runtime artifact manifest")
    allowlist = _load_json_object(Path(allowlist_path), "MCP Runtime artifact allowlist")
    metadata = mcp_runtime_artifact_metadata_from_manifest(manifest)
    allowed_checksums, allowed_cargo_lock_digests = artifact_allowlist_digests(
        allowlist,
        required_manifest=manifest,
    )
    validate_mcp_runtime_artifact_provenance(
        metadata,
        allowed_checksums=allowed_checksums,
        allowed_cargo_lock_digests=allowed_cargo_lock_digests,
    )
    return metadata, allowed_checksums, allowed_cargo_lock_digests


def mcp_runtime_artifact_metadata_from_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    if manifest.get("schema_version") != _ARTIFACT_MANIFEST_SCHEMA_VERSION:
        _raise_artifact_untrusted()
    if manifest.get("component") != _MCP_RUNTIME_COMPONENT:
        _raise_artifact_untrusted()
    contract_hashes = manifest.get("contract_hashes")
    proto_hashes = manifest.get("proto_hashes")
    if not isinstance(contract_hashes, Mapping) or not isinstance(proto_hashes, Mapping):
        _raise_artifact_untrusted()
    contract = load_mcp_runtime_contract()
    artifact_id = str(manifest.get("artifact_id") or "")
    artifact_kind = str(manifest.get("artifact_kind") or "")
    if artifact_id != _MCP_RUNTIME_ARTIFACT_ID:
        _raise_artifact_untrusted()
    mapped_kind = _GENERIC_ARTIFACT_KIND_MAP.get((artifact_id, artifact_kind), artifact_kind)
    if mapped_kind not in _ALLOWED_ARTIFACT_KINDS:
        _raise_artifact_untrusted()
    return {
        "source": manifest.get("source"),
        "artifact_kind": mapped_kind,
        "checksum_sha256": manifest.get("artifact_sha256"),
        "cargo_lock_digest": manifest.get("cargo_lock_sha256"),
        "protocol_version": contract["protocol_version"],
        "schema_hash": contract_hashes.get("mcp_runtime"),
        "error_code_table_hash": contract_hashes.get("mcp_runtime_errors"),
        "proto_hash": proto_hashes.get("mcp"),
        "sbom_digest": manifest.get("sbom_sha256"),
        "provenance_attestation": manifest.get("provenance_sha256"),
    }


def artifact_allowlist_digests(
    allowlist: Mapping[str, Any],
    *,
    required_manifest: Mapping[str, Any],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if allowlist.get("schema_version") != _ARTIFACT_ALLOWLIST_SCHEMA_VERSION:
        _raise_artifact_untrusted()
    entries = allowlist.get("allowed_artifacts")
    if not isinstance(entries, list):
        _raise_artifact_untrusted()
    checksums: list[str] = []
    cargo_lock_digests: list[str] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            _raise_artifact_untrusted()
        checksum = entry.get("artifact_sha256") or entry.get("checksum_sha256")
        cargo_lock = entry.get("cargo_lock_sha256") or entry.get("cargo_lock_digest")
        if isinstance(checksum, str) and checksum:
            checksums.append(checksum)
        if isinstance(cargo_lock, str) and cargo_lock:
            cargo_lock_digests.append(cargo_lock)
    if not any(artifact_allowlist_entry_matches_manifest(entry, required_manifest) for entry in entries):
        _raise_artifact_untrusted()
    if not checksums or not cargo_lock_digests:
        _raise_artifact_untrusted()
    return tuple(sorted(set(checksums))), tuple(sorted(set(cargo_lock_digests)))


def artifact_allowlist_entry_matches_manifest(entry: object, manifest: Mapping[str, Any]) -> bool:
    if not isinstance(entry, Mapping):
        return False
    if any(entry.get(field) != manifest.get(field) for field in _EXACT_ALLOWLIST_FIELDS):
        return False
    return all(entry.get(field) == manifest.get(field) for field in _NESTED_ALLOWLIST_FIELDS)


def validate_mcp_runtime_conformance_report(report: Mapping[str, Any]) -> dict[str, str]:
    if not isinstance(report, Mapping):
        _raise_conformance_blocked()
    if str(report.get("mcp_spec_version") or "") != "2025-11-25":
        _raise_conformance_blocked()
    phase_results = report.get("phase_results")
    if not isinstance(phase_results, Mapping):
        _raise_conformance_blocked()
    _require_boolean_evidence(phase_results, _CONFORMANCE_PHASES, _raise_conformance_blocked)
    if report.get("jsonrpc_batch_rejected") is not True:
        _raise_conformance_blocked()
    if report.get("raw_id_redaction_passed") is not True:
        _raise_conformance_blocked()
    return {
        "mcp_spec_version": "2025-11-25",
        "phase_results": ",".join(_CONFORMANCE_PHASES),
    }


def validate_mcp_runtime_benchmark_report(report: Mapping[str, Any]) -> dict[str, str]:
    if not isinstance(report, Mapping):
        _raise_benchmark_invalid()
    for baseline in _BENCHMARK_BASELINES:
        baseline_report = report.get(baseline)
        if not isinstance(baseline_report, Mapping):
            _raise_benchmark_invalid()
        for operation in _BENCHMARK_OPERATIONS:
            metrics = baseline_report.get(operation)
            if not isinstance(metrics, Mapping):
                _raise_benchmark_invalid()
            for metric in _BENCHMARK_METRICS:
                if not _non_negative_number(metrics.get(metric)):
                    _raise_benchmark_invalid()
    return {
        "baselines": ",".join(_BENCHMARK_BASELINES),
        "operations": ",".join(_BENCHMARK_OPERATIONS),
        "metrics": ",".join(_BENCHMARK_METRICS),
    }


def validate_mcp_runtime_promotion_readiness(report: Mapping[str, Any]) -> dict[str, str]:
    if not isinstance(report, Mapping):
        _raise_promotion_blocked()
    if str(report.get("scope") or "") not in {"mcp_runtime", "mcp_tool_subset", "mcp_server"}:
        _raise_promotion_blocked()
    if not _number_at_least(report.get("shadow_days"), 7):
        _raise_promotion_blocked()
    if not _number_at_least(report.get("shadow_samples"), 1000):
        _raise_promotion_blocked()
    for zero_field in (
        "contract_mismatch_count",
        "panic_or_crash_count",
        "raw_leak_count",
        "identity_mismatch_count",
    ):
        if not _number_at_most(report.get(zero_field), 0):
            _raise_promotion_blocked()
    legacy_p95 = report.get("python_legacy_p95_ms")
    rust_p95 = report.get("rust_sidecar_p95_ms")
    if not (_is_number(legacy_p95) and legacy_p95 > 0 and _is_number(rust_p95) and rust_p95 <= legacy_p95 * 1.10):
        _raise_promotion_blocked()
    if not _number_at_most(report.get("rust_error_rate_ppm"), report.get("python_legacy_error_rate_ppm")):
        _raise_promotion_blocked()
    required = (
        "conformance_passed",
        "recovery_drill_passed",
        "rollback_drill_passed",
        "ops_ready",
        "shadow_side_effect_safety_passed",
    )
    _require_boolean_evidence(report.get("evidence"), required, _raise_promotion_blocked)
    return {
        "promotion": "ready",
        "scope": str(report["scope"]),
        "shadow_days": str(report["shadow_days"]),
        "shadow_samples": str(report["shadow_samples"]),
    }


def validate_mcp_runtime_ops_readiness(report: Mapping[str, Any]) -> dict[str, str]:
    if not isinstance(report, Mapping):
        _raise_ops_blocked()
    _require_boolean_evidence(report.get("observability"), _OPS_OBSERVABILITY, _raise_ops_blocked)
    _require_boolean_evidence(report.get("alerts"), _OPS_ALERTS, _raise_ops_blocked)
    _require_boolean_evidence(report.get("drills"), _OPS_DRILLS, _raise_ops_blocked)
    return {
        "ops": "ready",
        "observability": ",".join(_OPS_OBSERVABILITY),
        "alerts": ",".join(_OPS_ALERTS),
        "drills": ",".join(_OPS_DRILLS),
    }


def validate_mcp_runtime_recovery_readiness(report: Mapping[str, Any]) -> dict[str, str]:
    if not isinstance(report, Mapping):
        _raise_recovery_blocked()
    _require_boolean_evidence(report.get("evidence"), _RECOVERY_EVIDENCE, _raise_recovery_blocked)
    return {"recovery": "ready", "evidence": ",".join(_RECOVERY_EVIDENCE)}


def validate_mcp_runtime_decommission_readiness(report: Mapping[str, Any]) -> dict[str, str]:
    if not isinstance(report, Mapping):
        _raise_decommission_blocked()
    if report.get("canonical_mcp_runtime_stable") is not True:
        _raise_decommission_blocked()
    if report.get("rollback_path") not in {"deployment_rollback", "legacy_mcp_runtime_flag"}:
        _raise_decommission_blocked()
    _require_boolean_evidence(report.get("legacy_paths_removed"), _LEGACY_REMOVED, _raise_decommission_blocked)
    _require_boolean_evidence(report.get("facade_only_paths"), _FACADE_ONLY, _raise_decommission_blocked)
    _require_boolean_evidence(report.get("evidence"), _DECOMMISSION_EVIDENCE, _raise_decommission_blocked)
    return {
        "decommission": "ready",
        "rollback_path": str(report["rollback_path"]),
        "removed_legacy_paths": ",".join(_LEGACY_REMOVED),
    }


def _load_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"mcp_runtime_artifact_untrusted: {label} is unavailable or invalid") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"mcp_runtime_artifact_untrusted: {label} must be a JSON object")
    return payload


def _non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _non_negative_number(value: Any) -> bool:
    return _is_number(value) and value >= 0


def _number_at_least(value: Any, lower_bound: int | float) -> bool:
    return _is_number(value) and value >= lower_bound


def _number_at_most(value: Any, upper_bound: Any) -> bool:
    return _is_number(value) and _is_number(upper_bound) and value <= upper_bound


def _require_boolean_evidence(evidence: Any, required_items: tuple[str, ...], error_factory: Any) -> None:
    if not isinstance(evidence, Mapping) or any(evidence.get(item) is not True for item in required_items):
        error_factory()


def _raise_artifact_untrusted() -> None:
    raise RuntimeError("mcp_runtime_artifact_untrusted: Rust MCP Runtime artifact provenance is not trusted")


def _raise_conformance_blocked() -> None:
    raise RuntimeError("mcp_runtime_conformance_blocked: Rust MCP Runtime conformance evidence is incomplete")


def _raise_benchmark_invalid() -> None:
    raise RuntimeError("mcp_runtime_benchmark_invalid: Rust MCP Runtime benchmark report is incomplete")


def _raise_promotion_blocked() -> None:
    raise RuntimeError("mcp_runtime_promotion_blocked: Rust MCP Runtime promotion threshold is not satisfied")


def _raise_ops_blocked() -> None:
    raise RuntimeError("mcp_runtime_ops_readiness_blocked: Rust MCP Runtime ops readiness evidence is incomplete")


def _raise_recovery_blocked() -> None:
    raise RuntimeError("mcp_runtime_recovery_readiness_blocked: Rust MCP Runtime recovery evidence is incomplete")


def _raise_decommission_blocked() -> None:
    raise RuntimeError("mcp_runtime_decommission_blocked: Rust MCP Runtime legacy decommission evidence is incomplete")


__all__ = [
    "artifact_allowlist_digests",
    "artifact_allowlist_entry_matches_manifest",
    "load_mcp_runtime_artifact_trust",
    "mcp_runtime_artifact_metadata_from_manifest",
    "validate_mcp_runtime_artifact_provenance",
    "validate_mcp_runtime_benchmark_report",
    "validate_mcp_runtime_conformance_report",
    "validate_mcp_runtime_decommission_readiness",
    "validate_mcp_runtime_ops_readiness",
    "validate_mcp_runtime_promotion_readiness",
    "validate_mcp_runtime_recovery_readiness",
]
