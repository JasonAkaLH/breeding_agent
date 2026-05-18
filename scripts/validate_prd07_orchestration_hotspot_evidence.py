#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

DEFAULT_EVIDENCE = Path("docs/prd/rust/evidence/prd07/orchestration_hotspot_release_gates.json")
SCHEMA_VERSION = "maf.prd07.orchestration_hotspot_evidence.v1"
GUARDED_STATUS = "conditional_candidate_not_started"
READY_STATUS = "candidate_ready_to_start"

REQUIRED_ALLOWED_SCOPE = (
    "dag_validator",
    "scheduler_policy",
    "completion_policy",
    "backpressure",
    "payload_policy",
    "token_budget_kernel",
    "artifact_dependency_sanitizer",
    "large_payload_parser",
    "optional_frontend_wasm_parser",
)
REQUIRED_EXCLUDED_SCOPE = (
    "llm_planner_prompt",
    "provider_fallback",
    "router_glue",
    "product_answer_strategy",
    "react_or_ant_design_ui",
)
REQUIRED_STARTUP_GATES = (
    "core_lifecycle_stable",
    "store_event_shadow_compare_stable",
    "skill_mcp_boundaries_stable",
    "performance_or_reliability_evidence",
    "python_baseline_defined",
    "candidate_baseline_plan_defined",
    "shadow_compare_plan_defined",
    "supply_chain_gates_defined",
    "benchmark_slo_gates_defined",
    "migration_dr_runbook_defined",
    "ops_runbook_defined",
    "legacy_decommission_plan_defined",
)
REQUIRED_FUTURE_RELEASE_GATES = (
    "artifact_provenance_required",
    "sbom_required",
    "allowlist_required",
    "python_js_baseline_required",
    "rust_wasm_candidate_baseline_required",
    "ffi_or_wasm_overhead_required",
    "p50_p95_p99_cpu_memory_payload_required",
    "state_migration_lock_backup_restore_required",
    "dashboard_alert_slo_runbook_drill_required",
    "legacy_duplicate_semantics_decommission_required",
)


class EvidenceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EvidenceError("prd07_orchestration_hotspot_evidence_invalid", f"{path} is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise EvidenceError("prd07_orchestration_hotspot_evidence_invalid", f"{path} must contain a JSON object")
    return payload


def _as_mapping(payload: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = payload.get(key)
    if not isinstance(value, Mapping):
        raise EvidenceError("prd07_orchestration_hotspot_evidence_invalid", f"{key} must be a JSON object")
    return value


def _require_true(mapping: Mapping[str, Any], keys: Sequence[str], *, parent: str) -> dict[str, bool]:
    missing = [key for key in keys if mapping.get(key) is not True]
    if missing:
        raise EvidenceError(
            "prd07_orchestration_hotspot_evidence_invalid",
            f"{parent} booleans must be true: " + ",".join(missing),
        )
    return {key: True for key in keys}


def _validate_classification(payload: Mapping[str, Any]) -> dict[str, Any]:
    value = _as_mapping(payload, "classification")
    if value.get("conditional_candidate") is not True:
        raise EvidenceError("prd07_orchestration_hotspot_evidence_invalid", "classification.conditional_candidate must be true")
    if value.get("in_mandatory_rust_target_set") is not False:
        raise EvidenceError(
            "prd07_orchestration_hotspot_evidence_invalid",
            "classification.in_mandatory_rust_target_set must be false",
        )
    if value.get("implementation_prd_required") is not True:
        raise EvidenceError(
            "prd07_orchestration_hotspot_evidence_invalid",
            "classification.implementation_prd_required must be true",
        )
    if value.get("candidate_crate_reserved") != "maf_orchestration_kernel":
        raise EvidenceError(
            "prd07_orchestration_hotspot_evidence_invalid",
            "classification.candidate_crate_reserved must be maf_orchestration_kernel",
        )
    if value.get("current_rust_kernel_started") is not False:
        raise EvidenceError(
            "prd07_orchestration_hotspot_evidence_invalid",
            "classification.current_rust_kernel_started must remain false until a separate implementation PRD is approved",
        )
    if Path("native/crates/maf_orchestration_kernel").exists():
        raise EvidenceError(
            "prd07_orchestration_hotspot_evidence_invalid",
            "native/crates/maf_orchestration_kernel exists before a PRD07 implementation startup gate is ready",
        )
    return dict(value)


def _validate_scope_boundary(payload: Mapping[str, Any]) -> dict[str, Any]:
    value = _as_mapping(payload, "scope_boundary")
    allowed = _as_mapping(value, "allowed_rust_scope")
    excluded = _as_mapping(value, "excluded_from_rust_scope")
    return {
        "allowed_rust_scope": _require_true(allowed, REQUIRED_ALLOWED_SCOPE, parent="scope_boundary.allowed_rust_scope"),
        "excluded_from_rust_scope": _require_true(
            excluded,
            REQUIRED_EXCLUDED_SCOPE,
            parent="scope_boundary.excluded_from_rust_scope",
        ),
    }


def _status_of(value: Any, *, gate: str) -> str:
    if not isinstance(value, Mapping):
        raise EvidenceError("prd07_orchestration_hotspot_evidence_invalid", f"startup_readiness.{gate} must be a JSON object")
    status = value.get("status")
    if status not in {"ready", "pending"}:
        raise EvidenceError(
            "prd07_orchestration_hotspot_evidence_invalid",
            f"startup_readiness.{gate}.status must be ready or pending",
        )
    return str(status)


def _validate_startup_readiness(
    payload: Mapping[str, Any],
    *,
    status: str,
    allow_pending: bool,
) -> tuple[dict[str, Any], list[str]]:
    value = _as_mapping(payload, "startup_readiness")
    results: dict[str, Any] = {}
    pending: list[str] = []
    for gate in REQUIRED_STARTUP_GATES:
        if gate not in value:
            raise EvidenceError("prd07_orchestration_hotspot_evidence_invalid", f"startup_readiness.{gate} is required")
        gate_value = value[gate]
        gate_status = _status_of(gate_value, gate=gate)
        results[gate] = dict(gate_value)  # type: ignore[arg-type]
        if gate_status != "ready":
            pending.append(gate)

    if status == READY_STATUS and pending and not allow_pending:
        raise EvidenceError(
            "prd07_orchestration_hotspot_evidence_pending",
            "pending startup gates remain: " + ", ".join(pending),
        )
    return results, pending


def _validate_baseline_tests(payload: Mapping[str, Any]) -> dict[str, list[str]]:
    value = _as_mapping(payload, "baseline_tests")
    results: dict[str, list[str]] = {}
    for section in ("python_orchestration", "token_counter", "main_agent_sanitizer"):
        paths = value.get(section)
        if not isinstance(paths, list) or not paths:
            raise EvidenceError("prd07_orchestration_hotspot_evidence_invalid", f"baseline_tests.{section} must be a non-empty list")
        normalized: list[str] = []
        for item in paths:
            if not isinstance(item, str) or not item:
                raise EvidenceError("prd07_orchestration_hotspot_evidence_invalid", f"baseline_tests.{section} items must be paths")
            if not Path(item).exists():
                raise EvidenceError("prd07_orchestration_hotspot_evidence_invalid", f"baseline test does not exist: {item}")
            normalized.append(item)
        results[section] = normalized
    return results


def _validate_future_release_gates(payload: Mapping[str, Any]) -> dict[str, bool]:
    return _require_true(
        _as_mapping(payload, "future_release_gates"),
        REQUIRED_FUTURE_RELEASE_GATES,
        parent="future_release_gates",
    )


def _validate_startup_blockers(payload: Mapping[str, Any], *, status: str, allow_pending: bool) -> list[str]:
    blockers = payload.get("startup_blockers", [])
    if not isinstance(blockers, list) or any(not isinstance(item, str) or not item for item in blockers):
        raise EvidenceError("prd07_orchestration_hotspot_evidence_invalid", "startup_blockers must be a list of strings")
    normalized = [str(item) for item in blockers]
    if status == READY_STATUS and normalized and not allow_pending:
        raise EvidenceError(
            "prd07_orchestration_hotspot_evidence_pending",
            "startup blockers remain: " + ", ".join(normalized),
        )
    return normalized


def validate_evidence(payload: Mapping[str, Any], *, allow_pending: bool) -> dict[str, Any]:
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise EvidenceError("prd07_orchestration_hotspot_evidence_invalid", "unsupported evidence schema_version")
    status = str(payload.get("status") or "")
    if status not in {GUARDED_STATUS, READY_STATUS}:
        raise EvidenceError(
            "prd07_orchestration_hotspot_evidence_invalid",
            f"status must be {GUARDED_STATUS} or {READY_STATUS}",
        )

    results: dict[str, Any] = {
        "classification": _validate_classification(payload),
        "scope_boundary": _validate_scope_boundary(payload),
        "baseline_tests": _validate_baseline_tests(payload),
        "future_release_gates": _validate_future_release_gates(payload),
    }
    startup_results, pending = _validate_startup_readiness(payload, status=status, allow_pending=allow_pending)
    results["startup_readiness"] = startup_results
    blockers = _validate_startup_blockers(payload, status=status, allow_pending=allow_pending)

    if status == GUARDED_STATUS:
        return {
            "status": "guarded",
            "pending_gates": [],
            "startup_blockers": blockers or pending,
            "results": results,
        }
    return {
        "status": "ready" if not pending and not blockers else "pending",
        "pending_gates": pending,
        "startup_blockers": blockers,
        "results": results,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate PRD07 orchestration hotspot conditional-candidate evidence.")
    parser.add_argument("--evidence", type=Path, default=DEFAULT_EVIDENCE)
    parser.add_argument(
        "--allow-pending",
        action="store_true",
        help="Treat future startup-readiness blockers as an explicit pending status.",
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
                    "startup_blockers": [f"{args.evidence} does not exist"],
                    "results": {},
                }
            else:
                raise EvidenceError("prd07_orchestration_hotspot_evidence_pending", f"{args.evidence} does not exist")
        else:
            result = validate_evidence(_load_json(args.evidence), allow_pending=args.allow_pending)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        elif result["status"] == "ready":
            print("prd07_orchestration_hotspot_evidence_ready")
        elif result["status"] == "guarded":
            print("prd07_orchestration_hotspot_evidence_guarded")
        else:
            print("prd07_orchestration_hotspot_evidence_pending: " + ",".join(result["pending_gates"]))
        return 0 if result["status"] in {"ready", "guarded"} or args.allow_pending else 1
    except EvidenceError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
