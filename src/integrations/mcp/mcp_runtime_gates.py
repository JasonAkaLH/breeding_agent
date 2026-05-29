from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from src.core.evidence import (
    is_number as _is_number,
    non_negative_number as _non_negative_number,
    number_at_least as _number_at_least,
    number_at_most as _number_at_most,
    require_boolean_evidence as _require_boolean_evidence,
)

from .protocol import (
    SUPPORTED_MCP_PROTOCOL_VERSION_ORDER,
    SUPPORTED_MCP_PROTOCOL_VERSIONS,
    mcp_remote_transport_family_for_protocol_version,
)
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
_CLIENT_CONFORMANCE_SCHEMA_VERSION = "maf.mcp.client_compatibility_conformance.v1"
_CONFORMANCE_GLOBAL_SAFETY_GATES = (
    "jsonrpc_batch_rejected",
    "raw_id_redaction_passed",
    "safe_diagnostics_passed",
)

_ALLOWED_CLIENT_ADAPTERS = ("official_rust_sdk", "python_legacy")
_MATRIX_SCHEMA_VERSION = "maf.mcp.client_compatibility_conformance_matrix.v1"
_SHADOW_COMPARE_SCHEMA_VERSION = "maf.mcp.official_rust_sdk_shadow_compare.v1"
_ENFORCE_ALLOWLIST_SCHEMA_VERSION = "maf.mcp.adapter_enforce_allowlist.v1"
_CONFORMANCE_TRANSPORT_SCOPE = "remote_http_only_until_stdio_sandbox_passes"
_MATRIX_REQUIRED_ADAPTER_FIELDS = (
    "initialize",
    "initialized",
    "tools_list",
    "tools_call",
    "redaction",
    "plaintext_http_audit",
    "redirect_origin_safety",
    "payload_size_limit",
)
_MATRIX_2024_REQUIRED_FIELDS = ("persistent_sse_response", "request_id_correlation")
_MATRIX_2025_REQUIRED_FIELDS = ("object_response", "sse_response")
_SHADOW_REQUIRED_COMPARED_FIELDS = frozenset(
    {
        "negotiated_protocol_version",
        "server_info",
        "capabilities",
        "tools_descriptor_shape",
        "safe_tool_call_result_shape",
        "error_category",
    }
)
_SHADOW_STATUSES = frozenset({"matched", "mismatched", "skipped"})
_OFFICIAL_SDK_OPERATIONAL_STATUSES = frozenset(
    {"passed", "partial_shadow_verified", "unsupported_transport", "pending_adapter_contract"}
)
_MATRIX_EVIDENCE_REFS_FIELD = "evidence_refs"
_ENFORCE_ROLLBACK_PATHS = frozenset({"python_legacy_adapter", "deployment_rollback", "legacy_mcp_runtime_flag"})

_CONFORMANCE_VERSION_GATES = (
    "initialize",
    "transport",
    "tools_list",
    "tools_call",
    "batch_rejected",
    "raw_id_redaction_passed",
    "safe_diagnostics_passed",
)
_FORBIDDEN_CONFORMANCE_FIELD_NAMES = frozenset(
    {
        "authorization",
        "endpoint",
        "last_event_id",
        "progress_token",
        "raw_endpoint",
        "raw_last_event_id",
        "raw_progress_token",
        "raw_session_id",
        "raw_tool_output",
        "session_id",
        "token",
        "tool_output",
    }
)
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
    if report.get("schema_version") != _CLIENT_CONFORMANCE_SCHEMA_VERSION:
        _raise_conformance_blocked()
    _reject_forbidden_conformance_fields(report)
    supported_versions = _string_tuple(report.get("supported_mcp_spec_versions"))
    if supported_versions != SUPPORTED_MCP_PROTOCOL_VERSION_ORDER:
        _raise_conformance_blocked()
    if set(supported_versions) != SUPPORTED_MCP_PROTOCOL_VERSIONS:
        _raise_conformance_blocked()
    phase_results = report.get("phase_results")
    if not isinstance(phase_results, Mapping):
        _raise_conformance_blocked()
    _require_boolean_evidence(phase_results, _CONFORMANCE_PHASES, _raise_conformance_blocked)
    _require_boolean_evidence(report, _CONFORMANCE_GLOBAL_SAFETY_GATES, _raise_conformance_blocked)
    version_results = report.get("version_results")
    if not isinstance(version_results, Mapping) or set(version_results.keys()) != set(
        SUPPORTED_MCP_PROTOCOL_VERSION_ORDER
    ):
        _raise_conformance_blocked()
    for version in SUPPORTED_MCP_PROTOCOL_VERSION_ORDER:
        result = version_results.get(version)
        if not isinstance(result, Mapping):
            _raise_conformance_blocked()
        _reject_forbidden_conformance_fields(result)
        if result.get("transport_family") != mcp_remote_transport_family_for_protocol_version(version):
            _raise_conformance_blocked()
        _require_boolean_evidence(result, _CONFORMANCE_VERSION_GATES, _raise_conformance_blocked)
    return {
        "supported_mcp_spec_versions": ",".join(SUPPORTED_MCP_PROTOCOL_VERSION_ORDER),
        "phase_results": ",".join(_CONFORMANCE_PHASES),
        "transport_families": "2024-11-05=legacy_http_sse,2025+=streamable_http",
        "version_results": ",".join(SUPPORTED_MCP_PROTOCOL_VERSION_ORDER),
    }


def validate_mcp_official_sdk_conformance_matrix(matrix: Mapping[str, Any]) -> dict[str, str]:
    if not isinstance(matrix, Mapping):
        _raise_conformance_blocked()
    if matrix.get("schema_version") != _MATRIX_SCHEMA_VERSION:
        _raise_conformance_blocked()
    _reject_forbidden_conformance_fields(matrix)
    supported_versions = _string_tuple(matrix.get("supported_mcp_spec_versions"))
    if supported_versions != SUPPORTED_MCP_PROTOCOL_VERSION_ORDER:
        _raise_conformance_blocked()
    if matrix.get("transport_scope") != _CONFORMANCE_TRANSPORT_SCOPE:
        _raise_conformance_blocked()
    if matrix.get("stdio_sandbox_conformance_passed") is not False:
        _raise_conformance_blocked()
    expected_transport_families = matrix.get("expected_transport_families")
    if not isinstance(expected_transport_families, Mapping):
        _raise_conformance_blocked()
    adapter_conformance = matrix.get("adapter_conformance")
    if not isinstance(adapter_conformance, Mapping) or set(adapter_conformance.keys()) != set(SUPPORTED_MCP_PROTOCOL_VERSION_ORDER):
        _raise_conformance_blocked()
    for version in SUPPORTED_MCP_PROTOCOL_VERSION_ORDER:
        expected_family = mcp_remote_transport_family_for_protocol_version(version)
        if expected_transport_families.get(version) != expected_family:
            _raise_conformance_blocked()
        version_entry = adapter_conformance.get(version)
        if not isinstance(version_entry, Mapping) or set(version_entry.keys()) != {expected_family}:
            _raise_conformance_blocked()
        transport_entry = version_entry.get(expected_family)
        if not isinstance(transport_entry, Mapping) or set(transport_entry.keys()) != set(_ALLOWED_CLIENT_ADAPTERS):
            _raise_conformance_blocked()
        for adapter in _ALLOWED_CLIENT_ADAPTERS:
            adapter_result = transport_entry.get(adapter)
            if not isinstance(adapter_result, Mapping):
                _raise_conformance_blocked()
            _require_existing_evidence_refs(adapter_result)
            required_fields = _MATRIX_REQUIRED_ADAPTER_FIELDS
            required_fields += _MATRIX_2024_REQUIRED_FIELDS if version == "2024-11-05" else _MATRIX_2025_REQUIRED_FIELDS
            if adapter == "python_legacy":
                _require_boolean_evidence(adapter_result, required_fields, _raise_conformance_blocked)
                continue
            operational_status = str(adapter_result.get("operational_status") or "")
            if operational_status not in _OFFICIAL_SDK_OPERATIONAL_STATUSES:
                _raise_conformance_blocked()
            shadow_compare = adapter_result.get("shadow_compare")
            if operational_status == "passed":
                _require_boolean_evidence(adapter_result, required_fields, _raise_conformance_blocked)
                if shadow_compare != "matched":
                    _raise_conformance_blocked()
            elif operational_status == "partial_shadow_verified":
                if version == "2024-11-05":
                    _raise_conformance_blocked()
                _require_boolean_evidence(
                    adapter_result,
                    _MATRIX_REQUIRED_ADAPTER_FIELDS + ("object_response",),
                    _raise_conformance_blocked,
                )
                if adapter_result.get("sse_response") is not False:
                    _raise_conformance_blocked()
                if shadow_compare != "matched" or adapter_result.get("enforce_allowed") is not False:
                    _raise_conformance_blocked()
                if not _non_empty_string(adapter_result.get("sse_response_gap_reason")):
                    _raise_conformance_blocked()
            else:
                if any(adapter_result.get(field) is True for field in required_fields):
                    _raise_conformance_blocked()
                if shadow_compare != "skipped" or adapter_result.get("enforce_allowed") is not False:
                    _raise_conformance_blocked()
                if not _non_empty_string(adapter_result.get("gap_reason")):
                    _raise_conformance_blocked()
    return {
        "supported_mcp_spec_versions": ",".join(SUPPORTED_MCP_PROTOCOL_VERSION_ORDER),
        "adapters": ",".join(_ALLOWED_CLIENT_ADAPTERS),
        "transport_scope": _CONFORMANCE_TRANSPORT_SCOPE,
    }


def validate_mcp_official_rust_sdk_shadow_compare(evidence: Mapping[str, Any]) -> dict[str, str]:
    if not isinstance(evidence, Mapping):
        _raise_conformance_blocked()
    if evidence.get("schema_version") != _SHADOW_COMPARE_SCHEMA_VERSION:
        _raise_conformance_blocked()
    _reject_forbidden_conformance_fields(evidence)
    if evidence.get("visible_adapter") != "python_legacy" or evidence.get("shadow_adapter") != "official_rust_sdk":
        _raise_conformance_blocked()
    results = evidence.get("results")
    if not isinstance(results, list) or not results:
        _raise_conformance_blocked()
    seen: set[str] = set()
    statuses: set[str] = set()
    for item in results:
        if not isinstance(item, Mapping):
            _raise_conformance_blocked()
        _reject_forbidden_conformance_fields(item)
        version = str(item.get("protocol_version") or "")
        if version not in SUPPORTED_MCP_PROTOCOL_VERSIONS:
            _raise_conformance_blocked()
        if item.get("transport_family") != mcp_remote_transport_family_for_protocol_version(version):
            _raise_conformance_blocked()
        status = str(item.get("status") or "")
        if status not in _SHADOW_STATUSES:
            _raise_conformance_blocked()
        if version in seen:
            _raise_conformance_blocked()
        skip_reason = item.get("skip_reason")
        if status == "skipped" and not _non_empty_string(skip_reason):
            _raise_conformance_blocked()
        if status != "skipped" and skip_reason is not None:
            _raise_conformance_blocked()
        if version == "2024-11-05" and status != "skipped":
            _raise_conformance_blocked()
        statuses.add(status)
        compared_fields = frozenset(str(field) for field in item.get("compared_fields") or ())
        if not _SHADOW_REQUIRED_COMPARED_FIELDS.issubset(compared_fields):
            _raise_conformance_blocked()
        redaction = item.get("redaction")
        if not isinstance(redaction, Mapping):
            _raise_conformance_blocked()
        if redaction.get("header_values") != "redacted" or redaction.get("raw_payload") != "omitted":
            _raise_conformance_blocked()
        if item.get("visible_path_unchanged") is not True:
            _raise_conformance_blocked()
        seen.add(version)
    if seen != set(SUPPORTED_MCP_PROTOCOL_VERSION_ORDER):
        _raise_conformance_blocked()
    return {
        "visible_adapter": "python_legacy",
        "shadow_adapter": "official_rust_sdk",
        "shadow_statuses": ",".join(sorted(statuses)),
    }


def validate_mcp_enforce_allowlist(allowlist: Mapping[str, Any]) -> dict[str, str]:
    if not isinstance(allowlist, Mapping):
        _raise_enforce_allowlist_blocked()
    if allowlist.get("schema_version") != _ENFORCE_ALLOWLIST_SCHEMA_VERSION:
        _raise_enforce_allowlist_blocked()
    _reject_forbidden_conformance_fields(allowlist)
    entries = allowlist.get("allowed_combinations")
    if not isinstance(entries, list) or not entries:
        _raise_enforce_allowlist_blocked()
    combinations: list[str] = []
    enforce_count = 0
    for entry in entries:
        if not isinstance(entry, Mapping):
            _raise_enforce_allowlist_blocked()
        version = str(entry.get("protocol_version") or "")
        adapter = str(entry.get("adapter") or "")
        server_scope = str(entry.get("server_scope") or "")
        transport_family = str(entry.get("transport_family") or "")
        if not server_scope or adapter not in _ALLOWED_CLIENT_ADAPTERS or version not in SUPPORTED_MCP_PROTOCOL_VERSIONS:
            _raise_enforce_allowlist_blocked()
        if transport_family != mcp_remote_transport_family_for_protocol_version(version):
            _raise_enforce_allowlist_blocked()
        shadow_status = str(entry.get("shadow_compare_status") or "")
        if shadow_status not in _SHADOW_STATUSES:
            _raise_enforce_allowlist_blocked()
        if entry.get("rollback_path") not in _ENFORCE_ROLLBACK_PATHS:
            _raise_enforce_allowlist_blocked()
        enforce_allowed = entry.get("enforce_allowed") is True
        if enforce_allowed and shadow_status != "matched":
            _raise_enforce_allowlist_blocked()
        if enforce_allowed:
            enforce_count += 1
        combinations.append(f"{server_scope}|{version}|{transport_family}|{adapter}|enforce={str(enforce_allowed).lower()}")
    return {
        "enforce_allowed_combinations": str(enforce_count),
        "combinations": ",".join(sorted(combinations)),
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


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    parsed = tuple(str(item) for item in value)
    return parsed if all(parsed) else ()


def _reject_forbidden_conformance_fields(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if str(key).strip().lower() in _FORBIDDEN_CONFORMANCE_FIELD_NAMES:
                _raise_conformance_blocked()
            _reject_forbidden_conformance_fields(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_forbidden_conformance_fields(nested)


def _non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _require_existing_evidence_refs(evidence: Mapping[str, Any]) -> None:
    refs = evidence.get(_MATRIX_EVIDENCE_REFS_FIELD)
    if not isinstance(refs, list) or not refs:
        _raise_conformance_blocked()
    for ref in refs:
        if not _non_empty_string(ref):
            _raise_conformance_blocked()
        path_text = ref.split("::", 1)[0].strip()
        if not path_text or Path(path_text).is_absolute() or ".." in Path(path_text).parts:
            _raise_conformance_blocked()
        if not Path(path_text).is_file():
            _raise_conformance_blocked()


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


def _raise_enforce_allowlist_blocked() -> None:
    raise RuntimeError("mcp_runtime_enforce_allowlist_blocked: MCP adapter enforce allowlist evidence is incomplete")


__all__ = [
    "artifact_allowlist_digests",
    "artifact_allowlist_entry_matches_manifest",
    "load_mcp_runtime_artifact_trust",
    "mcp_runtime_artifact_metadata_from_manifest",
    "validate_mcp_enforce_allowlist",
    "validate_mcp_official_rust_sdk_shadow_compare",
    "validate_mcp_official_sdk_conformance_matrix",
    "validate_mcp_runtime_artifact_provenance",
    "validate_mcp_runtime_benchmark_report",
    "validate_mcp_runtime_conformance_report",
    "validate_mcp_runtime_decommission_readiness",
    "validate_mcp_runtime_ops_readiness",
    "validate_mcp_runtime_promotion_readiness",
    "validate_mcp_runtime_recovery_readiness",
]
