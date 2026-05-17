#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.storage.runtime_sidecar_facade import (
    validate_runtime_sidecar_artifact_provenance,
    validate_runtime_sidecar_benchmark_report,
    validate_runtime_sidecar_decommission_readiness,
    validate_runtime_sidecar_migration_plan,
    validate_runtime_sidecar_ops_readiness,
    validate_runtime_sidecar_promotion_readiness,
)


DEFAULT_EVIDENCE = Path("docs/prd/rust/evidence/prd03/runtime_sidecar_release_gates.json")
SCHEMA_VERSION = "maf.prd03.runtime_sidecar_evidence.v1"


class EvidenceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EvidenceError("prd03_runtime_sidecar_evidence_invalid", f"{path} is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise EvidenceError("prd03_runtime_sidecar_evidence_invalid", f"{path} must contain a JSON object")
    return payload


def _pending(value: Any) -> bool:
    return value is None or value == {} or (isinstance(value, Mapping) and value.get("status") == "pending")


def _validate_artifact(payload: Mapping[str, Any]) -> dict[str, str]:
    metadata = payload.get("artifact_provenance")
    if _pending(metadata):
        raise EvidenceError("prd03_runtime_sidecar_evidence_pending", "artifact provenance evidence is pending")
    allowed_checksums = payload.get("allowed_artifact_checksums", [])
    allowed_cargo_lock_digests = payload.get("allowed_cargo_lock_digests", [])
    if not isinstance(allowed_checksums, list) or not isinstance(allowed_cargo_lock_digests, list):
        raise EvidenceError(
            "prd03_runtime_sidecar_evidence_invalid",
            "allowed artifact checksum and Cargo.lock digest lists are required",
        )
    return validate_runtime_sidecar_artifact_provenance(
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
        raise EvidenceError("prd03_runtime_sidecar_evidence_pending", f"{key} evidence is pending")
    if not isinstance(value, Mapping):
        raise EvidenceError("prd03_runtime_sidecar_evidence_invalid", f"{key} must be a JSON object")
    return validator(value)


def validate_evidence(payload: Mapping[str, Any], *, allow_pending: bool) -> dict[str, Any]:
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise EvidenceError("prd03_runtime_sidecar_evidence_invalid", "unsupported evidence schema_version")

    gate_specs: tuple[tuple[str, Callable[[], dict[str, str]]], ...] = (
        ("artifact_provenance", lambda: _validate_artifact(payload)),
        (
            "benchmark_report",
            lambda: _validate_required_mapping(
                payload,
                "benchmark_report",
                validate_runtime_sidecar_benchmark_report,
            ),
        ),
        (
            "promotion_readiness",
            lambda: _validate_required_mapping(
                payload,
                "promotion_readiness",
                validate_runtime_sidecar_promotion_readiness,
            ),
        ),
        (
            "migration_plan",
            lambda: _validate_required_mapping(
                payload,
                "migration_plan",
                validate_runtime_sidecar_migration_plan,
            ),
        ),
        (
            "ops_readiness",
            lambda: _validate_required_mapping(
                payload,
                "ops_readiness",
                validate_runtime_sidecar_ops_readiness,
            ),
        ),
        (
            "decommission_readiness",
            lambda: _validate_required_mapping(
                payload,
                "decommission_readiness",
                validate_runtime_sidecar_decommission_readiness,
            ),
        ),
    )

    results: dict[str, Any] = {}
    pending: list[str] = []
    for gate, check in gate_specs:
        try:
            results[gate] = check()
        except EvidenceError as exc:
            if exc.code == "prd03_runtime_sidecar_evidence_pending" and allow_pending:
                pending.append(gate)
                results[gate] = {"status": "pending", "reason": str(exc)}
                continue
            raise

    blockers = payload.get("blockers", [])
    if blockers and not allow_pending:
        raise EvidenceError(
            "prd03_runtime_sidecar_evidence_pending",
            "external blockers remain: " + ", ".join(str(item) for item in blockers),
        )
    if pending and not allow_pending:
        raise EvidenceError(
            "prd03_runtime_sidecar_evidence_pending",
            "pending gates remain: " + ", ".join(pending),
        )
    return {
        "status": "ready" if not pending and not blockers else "pending",
        "pending_gates": pending,
        "blockers": blockers,
        "results": results,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate PRD03 RuntimeSidecar release-gate evidence.")
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
                raise EvidenceError("prd03_runtime_sidecar_evidence_pending", f"{args.evidence} does not exist")
        else:
            result = validate_evidence(_load_json(args.evidence), allow_pending=args.allow_pending)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        elif result["status"] == "ready":
            print("prd03_runtime_sidecar_evidence_ready")
        else:
            print("prd03_runtime_sidecar_evidence_pending: " + ",".join(result["pending_gates"]))
        return 0 if result["status"] == "ready" or args.allow_pending else 1
    except (EvidenceError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
