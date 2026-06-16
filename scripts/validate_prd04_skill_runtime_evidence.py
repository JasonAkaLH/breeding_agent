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

_AGENT_SKILLS_DIR = REPO_ROOT / "src" / "integrations" / "agent_skills"
_LIGHTWEIGHT_PACKAGE = "_maf_prd04_agent_skills"

_rust_contract = load_lightweight_module(
    package_name=_LIGHTWEIGHT_PACKAGE,
    module_dir=_AGENT_SKILLS_DIR,
    name="rust_contract",
    error_label="Skill Runtime",
)
_skill_runtime_gates = load_lightweight_module(
    package_name=_LIGHTWEIGHT_PACKAGE,
    module_dir=_AGENT_SKILLS_DIR,
    name="skill_runtime_gates",
    error_label="Skill Runtime",
)

load_skill_runtime_contract = _rust_contract.load_skill_runtime_contract
validate_skill_runtime_artifact_provenance = _skill_runtime_gates.validate_skill_runtime_artifact_provenance
validate_skill_runtime_benchmark_report = _skill_runtime_gates.validate_skill_runtime_benchmark_report
validate_skill_runtime_decommission_readiness = _skill_runtime_gates.validate_skill_runtime_decommission_readiness
validate_skill_runtime_ops_readiness = _skill_runtime_gates.validate_skill_runtime_ops_readiness
validate_skill_runtime_promotion_readiness = _skill_runtime_gates.validate_skill_runtime_promotion_readiness

DEFAULT_EVIDENCE = Path("docs/prd/rust/evidence/prd04/skill_runtime_release_gates.json")
SCHEMA_VERSION = "maf.prd04.skill_runtime_evidence.v1"
INVALID_CODE = "prd04_skill_runtime_evidence_invalid"
PENDING_CODE = "prd04_skill_runtime_evidence_pending"
_REQUIRED_ARTIFACTS = {
    "skill_policy_wheel": "skill_policy_pyo3_wheel",
    "skill_sandbox_sidecar": "skill_sandbox_sidecar_binary",
}
_GENERIC_ARTIFACT_KIND_MAP = {
    ("maf_skill_runtime_pyo3", "pyo3_wheel"): "skill_policy_pyo3_wheel",
    ("maf_skill_sandbox", "sidecar_binary"): "skill_sandbox_sidecar_binary",
}


def _artifact_metadata_from_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    contract_hashes = manifest.get("contract_hashes")
    if not isinstance(contract_hashes, Mapping):
        raise EvidenceError(INVALID_CODE, "Skill Runtime artifact manifest must include contract_hashes")
    artifact_id = str(manifest.get("artifact_id") or "")
    artifact_kind = str(manifest.get("artifact_kind") or "")
    mapped_kind = _GENERIC_ARTIFACT_KIND_MAP.get((artifact_id, artifact_kind), artifact_kind)
    contract = load_skill_runtime_contract()
    return {
        "source": manifest.get("source"),
        "artifact_kind": mapped_kind,
        "checksum_sha256": manifest.get("artifact_sha256"),
        "cargo_lock_digest": manifest.get("cargo_lock_sha256"),
        "contract_version": contract["contract_version"],
        "bundle_revision": manifest.get("git_commit") or manifest.get("artifact_name"),
        "schema_hash": contract_hashes.get("skill_runtime"),
        "sbom_digest": manifest.get("sbom_sha256"),
        "provenance_attestation": manifest.get("provenance_sha256"),
    }


def _normalize_artifact_metadata(value: Mapping[str, Any]) -> dict[str, Any]:
    if "artifact_sha256" in value or "cargo_lock_sha256" in value:
        return _artifact_metadata_from_manifest(value)
    return dict(value)


def _validate_artifacts(payload: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    artifacts = payload.get("artifact_provenance")
    if is_pending(artifacts):
        raise EvidenceError(PENDING_CODE, "artifact provenance evidence is pending")
    if not isinstance(artifacts, Mapping):
        raise EvidenceError(INVALID_CODE, "artifact_provenance must be a JSON object")
    allowed_checksums, allowed_cargo_lock_digests = allowed_digest_sets(payload, invalid_code=INVALID_CODE)
    results: dict[str, dict[str, str]] = {}
    for key, expected_kind in _REQUIRED_ARTIFACTS.items():
        value = artifacts.get(key)
        if is_pending(value):
            raise EvidenceError(PENDING_CODE, f"{key} artifact evidence is pending")
        if not isinstance(value, Mapping):
            raise EvidenceError(INVALID_CODE, f"{key} artifact evidence must be a JSON object")
        metadata = _normalize_artifact_metadata(value)
        if metadata.get("artifact_kind") != expected_kind:
            raise EvidenceError(INVALID_CODE, f"{key} artifact_kind must be {expected_kind}")
        results[key] = validate_skill_runtime_artifact_provenance(
            metadata,
            allowed_checksums=allowed_checksums,
            allowed_cargo_lock_digests=allowed_cargo_lock_digests,
        )
    return results


def validate_evidence(payload: Mapping[str, Any], *, allow_pending: bool) -> dict[str, Any]:
    validate_schema_version(payload, expected=SCHEMA_VERSION, invalid_code=INVALID_CODE)

    gate_specs: tuple[GateSpec, ...] = (
        ("artifact_provenance", lambda: _validate_artifacts(payload)),
        (
            "benchmark_report",
            lambda: required_mapping(
                payload,
                "benchmark_report",
                validate_skill_runtime_benchmark_report,
                pending_code=PENDING_CODE,
                invalid_code=INVALID_CODE,
            ),
        ),
        (
            "promotion_readiness",
            lambda: required_mapping(
                payload,
                "promotion_readiness",
                validate_skill_runtime_promotion_readiness,
                pending_code=PENDING_CODE,
                invalid_code=INVALID_CODE,
            ),
        ),
        (
            "ops_readiness",
            lambda: required_mapping(
                payload,
                "ops_readiness",
                validate_skill_runtime_ops_readiness,
                pending_code=PENDING_CODE,
                invalid_code=INVALID_CODE,
            ),
        ),
        (
            "decommission_readiness",
            lambda: required_mapping(
                payload,
                "decommission_readiness",
                validate_skill_runtime_decommission_readiness,
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
        description="Validate PRD04 Skill Runtime release-gate evidence.",
        invalid_code=INVALID_CODE,
        missing_pending_code=PENDING_CODE,
        status_messages={"ready": "prd04_skill_runtime_evidence_ready"},
        pending_message_prefix="prd04_skill_runtime_evidence_pending",
    )


if __name__ == "__main__":
    raise SystemExit(main())
