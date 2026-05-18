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
    load_lightweight_module,
    required_mapping,
    run_evidence_cli,
    validate_schema_version,
)

_MCP_DIR = REPO_ROOT / "src" / "integrations" / "mcp"
_LIGHTWEIGHT_PACKAGE = "_maf_prd05_mcp"

_mcp_runtime_gates = load_lightweight_module(
    package_name=_LIGHTWEIGHT_PACKAGE,
    module_dir=_MCP_DIR,
    name="mcp_runtime_gates",
    error_label="MCP Runtime",
)

validate_mcp_runtime_artifact_provenance = _mcp_runtime_gates.validate_mcp_runtime_artifact_provenance
validate_mcp_runtime_benchmark_report = _mcp_runtime_gates.validate_mcp_runtime_benchmark_report
validate_mcp_runtime_conformance_report = _mcp_runtime_gates.validate_mcp_runtime_conformance_report
validate_mcp_runtime_decommission_readiness = _mcp_runtime_gates.validate_mcp_runtime_decommission_readiness
validate_mcp_runtime_ops_readiness = _mcp_runtime_gates.validate_mcp_runtime_ops_readiness
validate_mcp_runtime_promotion_readiness = _mcp_runtime_gates.validate_mcp_runtime_promotion_readiness
validate_mcp_runtime_recovery_readiness = _mcp_runtime_gates.validate_mcp_runtime_recovery_readiness
mcp_runtime_artifact_metadata_from_manifest = _mcp_runtime_gates.mcp_runtime_artifact_metadata_from_manifest

DEFAULT_EVIDENCE = Path("docs/prd/rust/evidence/prd05/mcp_runtime_release_gates.json")
SCHEMA_VERSION = "maf.prd05.mcp_runtime_evidence.v1"
INVALID_CODE = "prd05_mcp_runtime_evidence_invalid"
PENDING_CODE = "prd05_mcp_runtime_evidence_pending"
_GENERIC_ARTIFACT_KIND_MAP = {
    ("maf_mcp_runtime_sidecar", "sidecar_binary"): "mcp_runtime_sidecar_binary",
}


def _normalize_artifact_metadata(value: Mapping[str, Any]) -> dict[str, Any]:
    if "artifact_sha256" in value or "cargo_lock_sha256" in value:
        metadata = mcp_runtime_artifact_metadata_from_manifest(value)
        artifact_id = str(value.get("artifact_id") or "")
        artifact_kind = str(value.get("artifact_kind") or "")
        metadata["artifact_kind"] = _GENERIC_ARTIFACT_KIND_MAP.get(
            (artifact_id, artifact_kind),
            metadata["artifact_kind"],
        )
        return metadata
    return dict(value)


def _validate_artifact(payload: Mapping[str, Any]) -> dict[str, str]:
    value = payload.get("artifact_provenance")
    if is_pending(value):
        raise EvidenceError(PENDING_CODE, "artifact provenance evidence is pending")
    if not isinstance(value, Mapping):
        raise EvidenceError(INVALID_CODE, "artifact_provenance must be a JSON object")
    allowed_checksums, allowed_cargo_lock_digests = allowed_digest_sets(payload, invalid_code=INVALID_CODE)
    metadata = _normalize_artifact_metadata(value)
    if metadata.get("artifact_kind") != "mcp_runtime_sidecar_binary":
        raise EvidenceError(
            INVALID_CODE,
            "artifact_provenance.artifact_kind must be mcp_runtime_sidecar_binary",
        )
    return validate_mcp_runtime_artifact_provenance(
        metadata,
        allowed_checksums=allowed_checksums,
        allowed_cargo_lock_digests=allowed_cargo_lock_digests,
    )


def validate_evidence(payload: Mapping[str, Any], *, allow_pending: bool) -> dict[str, Any]:
    validate_schema_version(payload, expected=SCHEMA_VERSION, invalid_code=INVALID_CODE)

    gate_specs: tuple[GateSpec, ...] = (
        ("artifact_provenance", lambda: _validate_artifact(payload)),
        (
            "conformance_report",
            lambda: required_mapping(
                payload,
                "conformance_report",
                validate_mcp_runtime_conformance_report,
                pending_code=PENDING_CODE,
                invalid_code=INVALID_CODE,
            ),
        ),
        (
            "benchmark_report",
            lambda: required_mapping(
                payload,
                "benchmark_report",
                validate_mcp_runtime_benchmark_report,
                pending_code=PENDING_CODE,
                invalid_code=INVALID_CODE,
            ),
        ),
        (
            "promotion_readiness",
            lambda: required_mapping(
                payload,
                "promotion_readiness",
                validate_mcp_runtime_promotion_readiness,
                pending_code=PENDING_CODE,
                invalid_code=INVALID_CODE,
            ),
        ),
        (
            "ops_readiness",
            lambda: required_mapping(
                payload,
                "ops_readiness",
                validate_mcp_runtime_ops_readiness,
                pending_code=PENDING_CODE,
                invalid_code=INVALID_CODE,
            ),
        ),
        (
            "recovery_readiness",
            lambda: required_mapping(
                payload,
                "recovery_readiness",
                validate_mcp_runtime_recovery_readiness,
                pending_code=PENDING_CODE,
                invalid_code=INVALID_CODE,
            ),
        ),
        (
            "decommission_readiness",
            lambda: required_mapping(
                payload,
                "decommission_readiness",
                validate_mcp_runtime_decommission_readiness,
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
        description="Validate PRD05 MCP Runtime release-gate evidence.",
        invalid_code=INVALID_CODE,
        missing_pending_code=PENDING_CODE,
        status_messages={"ready": "prd05_mcp_runtime_evidence_ready"},
        pending_message_prefix="prd05_mcp_runtime_evidence_pending",
    )


if __name__ == "__main__":
    raise SystemExit(main())
