#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import types
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_CODEX_SKILLS_DIR = REPO_ROOT / "src" / "integrations" / "codex_skills"
_LIGHTWEIGHT_PACKAGE = "_maf_prd04_codex_skills"


def _load_lightweight_module(name: str) -> Any:
    """Load Skill Runtime gate helpers without importing optional integration deps.

    The GitHub Rust quality job intentionally runs this validator before Python
    application dependencies such as PyYAML are installed. Importing
    ``src.integrations.codex_skills`` would execute ``src.integrations`` package
    initializers and pull those optional dependencies in. These gate helpers are
    stdlib-only, so load just the two files under a private package namespace.
    """

    package = sys.modules.get(_LIGHTWEIGHT_PACKAGE)
    if package is None:
        package = types.ModuleType(_LIGHTWEIGHT_PACKAGE)
        package.__path__ = [str(_CODEX_SKILLS_DIR)]  # type: ignore[attr-defined]
        sys.modules[_LIGHTWEIGHT_PACKAGE] = package

    module_name = f"{_LIGHTWEIGHT_PACKAGE}.{name}"
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, _CODEX_SKILLS_DIR / f"{name}.py")
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load Skill Runtime helper module: {name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


_rust_contract = _load_lightweight_module("rust_contract")
_skill_runtime_gates = _load_lightweight_module("skill_runtime_gates")

load_skill_runtime_contract = _rust_contract.load_skill_runtime_contract
validate_skill_runtime_artifact_provenance = _skill_runtime_gates.validate_skill_runtime_artifact_provenance
validate_skill_runtime_benchmark_report = _skill_runtime_gates.validate_skill_runtime_benchmark_report
validate_skill_runtime_decommission_readiness = _skill_runtime_gates.validate_skill_runtime_decommission_readiness
validate_skill_runtime_ops_readiness = _skill_runtime_gates.validate_skill_runtime_ops_readiness
validate_skill_runtime_promotion_readiness = _skill_runtime_gates.validate_skill_runtime_promotion_readiness

DEFAULT_EVIDENCE = Path("docs/prd/rust/evidence/prd04/skill_runtime_release_gates.json")
SCHEMA_VERSION = "maf.prd04.skill_runtime_evidence.v1"
_REQUIRED_ARTIFACTS = {
    "skill_policy_wheel": "skill_policy_pyo3_wheel",
    "skill_sandbox_sidecar": "skill_sandbox_sidecar_binary",
}
_GENERIC_ARTIFACT_KIND_MAP = {
    ("maf_skill_runtime_pyo3", "pyo3_wheel"): "skill_policy_pyo3_wheel",
    ("maf_skill_sandbox", "sidecar_binary"): "skill_sandbox_sidecar_binary",
}


class EvidenceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EvidenceError("prd04_skill_runtime_evidence_invalid", f"{path} is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise EvidenceError("prd04_skill_runtime_evidence_invalid", f"{path} must contain a JSON object")
    return payload


def _pending(value: Any) -> bool:
    return value is None or value == {} or (isinstance(value, Mapping) and value.get("status") == "pending")


def _artifact_metadata_from_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    contract_hashes = manifest.get("contract_hashes")
    if not isinstance(contract_hashes, Mapping):
        raise EvidenceError(
            "prd04_skill_runtime_evidence_invalid",
            "Skill Runtime artifact manifest must include contract_hashes",
        )
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
    if _pending(artifacts):
        raise EvidenceError("prd04_skill_runtime_evidence_pending", "artifact provenance evidence is pending")
    if not isinstance(artifacts, Mapping):
        raise EvidenceError("prd04_skill_runtime_evidence_invalid", "artifact_provenance must be a JSON object")
    allowed_checksums = payload.get("allowed_artifact_checksums", [])
    allowed_cargo_lock_digests = payload.get("allowed_cargo_lock_digests", [])
    if not isinstance(allowed_checksums, list) or not isinstance(allowed_cargo_lock_digests, list):
        raise EvidenceError(
            "prd04_skill_runtime_evidence_invalid",
            "allowed artifact checksum and Cargo.lock digest lists are required",
        )
    results: dict[str, dict[str, str]] = {}
    for key, expected_kind in _REQUIRED_ARTIFACTS.items():
        value = artifacts.get(key)
        if _pending(value):
            raise EvidenceError("prd04_skill_runtime_evidence_pending", f"{key} artifact evidence is pending")
        if not isinstance(value, Mapping):
            raise EvidenceError("prd04_skill_runtime_evidence_invalid", f"{key} artifact evidence must be a JSON object")
        metadata = _normalize_artifact_metadata(value)
        if metadata.get("artifact_kind") != expected_kind:
            raise EvidenceError(
                "prd04_skill_runtime_evidence_invalid",
                f"{key} artifact_kind must be {expected_kind}",
            )
        results[key] = validate_skill_runtime_artifact_provenance(
            metadata,
            allowed_checksums=set(str(item) for item in allowed_checksums),
            allowed_cargo_lock_digests=set(str(item) for item in allowed_cargo_lock_digests),
        )
    return results


def _validate_required_mapping(
    payload: Mapping[str, Any],
    key: str,
    validator: Callable[[Mapping[str, Any]], dict[str, str]],
) -> dict[str, str]:
    value = payload.get(key)
    if _pending(value):
        raise EvidenceError("prd04_skill_runtime_evidence_pending", f"{key} evidence is pending")
    if not isinstance(value, Mapping):
        raise EvidenceError("prd04_skill_runtime_evidence_invalid", f"{key} must be a JSON object")
    return validator(value)


def validate_evidence(payload: Mapping[str, Any], *, allow_pending: bool) -> dict[str, Any]:
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise EvidenceError("prd04_skill_runtime_evidence_invalid", "unsupported evidence schema_version")

    gate_specs: tuple[tuple[str, Callable[[], dict[str, Any]]], ...] = (
        ("artifact_provenance", lambda: _validate_artifacts(payload)),
        (
            "benchmark_report",
            lambda: _validate_required_mapping(payload, "benchmark_report", validate_skill_runtime_benchmark_report),
        ),
        (
            "promotion_readiness",
            lambda: _validate_required_mapping(payload, "promotion_readiness", validate_skill_runtime_promotion_readiness),
        ),
        ("ops_readiness", lambda: _validate_required_mapping(payload, "ops_readiness", validate_skill_runtime_ops_readiness)),
        (
            "decommission_readiness",
            lambda: _validate_required_mapping(
                payload,
                "decommission_readiness",
                validate_skill_runtime_decommission_readiness,
            ),
        ),
    )

    results: dict[str, Any] = {}
    pending: list[str] = []
    for gate, check in gate_specs:
        try:
            results[gate] = check()
        except EvidenceError as exc:
            if exc.code == "prd04_skill_runtime_evidence_pending" and allow_pending:
                pending.append(gate)
                results[gate] = {"status": "pending", "reason": str(exc)}
                continue
            raise

    blockers = payload.get("blockers", [])
    if blockers and not allow_pending:
        raise EvidenceError(
            "prd04_skill_runtime_evidence_pending",
            "external blockers remain: " + ", ".join(str(item) for item in blockers),
        )
    if pending and not allow_pending:
        raise EvidenceError("prd04_skill_runtime_evidence_pending", "pending gates remain: " + ", ".join(pending))
    return {
        "status": "ready" if not pending and not blockers else "pending",
        "pending_gates": pending,
        "blockers": blockers,
        "results": results,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate PRD04 Skill Runtime release-gate evidence.")
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
                raise EvidenceError("prd04_skill_runtime_evidence_pending", f"{args.evidence} does not exist")
        else:
            result = validate_evidence(_load_json(args.evidence), allow_pending=args.allow_pending)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        elif result["status"] == "ready":
            print("prd04_skill_runtime_evidence_ready")
        else:
            print("prd04_skill_runtime_evidence_pending: " + ",".join(result["pending_gates"]))
        return 0 if result["status"] == "ready" or args.allow_pending else 1
    except (EvidenceError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
