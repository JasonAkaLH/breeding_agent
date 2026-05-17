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
_MCP_DIR = REPO_ROOT / "src" / "integrations" / "mcp"
_LIGHTWEIGHT_PACKAGE = "_maf_prd05_mcp"


def _load_lightweight_module(name: str) -> Any:
    package = sys.modules.get(_LIGHTWEIGHT_PACKAGE)
    if package is None:
        package = types.ModuleType(_LIGHTWEIGHT_PACKAGE)
        package.__path__ = [str(_MCP_DIR)]  # type: ignore[attr-defined]
        sys.modules[_LIGHTWEIGHT_PACKAGE] = package
    module_name = f"{_LIGHTWEIGHT_PACKAGE}.{name}"
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, _MCP_DIR / f"{name}.py")
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load MCP Runtime helper module: {name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


_mcp_runtime_gates = _load_lightweight_module("mcp_runtime_gates")

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
_GENERIC_ARTIFACT_KIND_MAP = {
    ("maf_mcp_runtime_sidecar", "sidecar_binary"): "mcp_runtime_sidecar_binary",
}


class EvidenceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EvidenceError("prd05_mcp_runtime_evidence_invalid", f"{path} is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise EvidenceError("prd05_mcp_runtime_evidence_invalid", f"{path} must contain a JSON object")
    return payload


def _pending(value: Any) -> bool:
    return value is None or value == {} or (isinstance(value, Mapping) and value.get("status") == "pending")


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
    if _pending(value):
        raise EvidenceError("prd05_mcp_runtime_evidence_pending", "artifact provenance evidence is pending")
    if not isinstance(value, Mapping):
        raise EvidenceError("prd05_mcp_runtime_evidence_invalid", "artifact_provenance must be a JSON object")
    allowed_checksums = payload.get("allowed_artifact_checksums", [])
    allowed_cargo_lock_digests = payload.get("allowed_cargo_lock_digests", [])
    if not isinstance(allowed_checksums, list) or not isinstance(allowed_cargo_lock_digests, list):
        raise EvidenceError(
            "prd05_mcp_runtime_evidence_invalid",
            "allowed artifact checksum and Cargo.lock digest lists are required",
        )
    metadata = _normalize_artifact_metadata(value)
    if metadata.get("artifact_kind") != "mcp_runtime_sidecar_binary":
        raise EvidenceError(
            "prd05_mcp_runtime_evidence_invalid",
            "artifact_provenance.artifact_kind must be mcp_runtime_sidecar_binary",
        )
    return validate_mcp_runtime_artifact_provenance(
        metadata,
        allowed_checksums=set(str(item) for item in allowed_checksums),
        allowed_cargo_lock_digests=set(str(item) for item in allowed_cargo_lock_digests),
    )


def _validate_required_mapping(
    payload: Mapping[str, Any],
    key: str,
    validator: Callable[[Mapping[str, Any]], dict[str, str]],
) -> dict[str, str]:
    value = payload.get(key)
    if _pending(value):
        raise EvidenceError("prd05_mcp_runtime_evidence_pending", f"{key} evidence is pending")
    if not isinstance(value, Mapping):
        raise EvidenceError("prd05_mcp_runtime_evidence_invalid", f"{key} must be a JSON object")
    return validator(value)


def validate_evidence(payload: Mapping[str, Any], *, allow_pending: bool) -> dict[str, Any]:
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise EvidenceError("prd05_mcp_runtime_evidence_invalid", "unsupported evidence schema_version")

    gate_specs: tuple[tuple[str, Callable[[], dict[str, Any]]], ...] = (
        ("artifact_provenance", lambda: _validate_artifact(payload)),
        (
            "conformance_report",
            lambda: _validate_required_mapping(payload, "conformance_report", validate_mcp_runtime_conformance_report),
        ),
        (
            "benchmark_report",
            lambda: _validate_required_mapping(payload, "benchmark_report", validate_mcp_runtime_benchmark_report),
        ),
        (
            "promotion_readiness",
            lambda: _validate_required_mapping(
                payload,
                "promotion_readiness",
                validate_mcp_runtime_promotion_readiness,
            ),
        ),
        (
            "ops_readiness",
            lambda: _validate_required_mapping(payload, "ops_readiness", validate_mcp_runtime_ops_readiness),
        ),
        (
            "recovery_readiness",
            lambda: _validate_required_mapping(payload, "recovery_readiness", validate_mcp_runtime_recovery_readiness),
        ),
        (
            "decommission_readiness",
            lambda: _validate_required_mapping(
                payload,
                "decommission_readiness",
                validate_mcp_runtime_decommission_readiness,
            ),
        ),
    )

    results: dict[str, Any] = {}
    pending: list[str] = []
    for gate, check in gate_specs:
        try:
            results[gate] = check()
        except EvidenceError as exc:
            if exc.code == "prd05_mcp_runtime_evidence_pending" and allow_pending:
                pending.append(gate)
                results[gate] = {"status": "pending", "reason": str(exc)}
                continue
            raise

    blockers = payload.get("blockers", [])
    if blockers and not allow_pending:
        raise EvidenceError(
            "prd05_mcp_runtime_evidence_pending",
            "external blockers remain: " + ", ".join(str(item) for item in blockers),
        )
    if pending and not allow_pending:
        raise EvidenceError("prd05_mcp_runtime_evidence_pending", "pending gates remain: " + ", ".join(pending))
    return {
        "status": "ready" if not pending and not blockers else "pending",
        "pending_gates": pending,
        "blockers": blockers,
        "results": results,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate PRD05 MCP Runtime release-gate evidence.")
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
                raise EvidenceError("prd05_mcp_runtime_evidence_pending", f"{args.evidence} does not exist")
        else:
            result = validate_evidence(_load_json(args.evidence), allow_pending=args.allow_pending)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        elif result["status"] == "ready":
            print("prd05_mcp_runtime_evidence_ready")
        else:
            print("prd05_mcp_runtime_evidence_pending: " + ",".join(result["pending_gates"]))
        return 0 if result["status"] == "ready" or args.allow_pending else 1
    except (EvidenceError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
