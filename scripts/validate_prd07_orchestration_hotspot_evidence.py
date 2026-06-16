#!/usr/bin/env python3
from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.prd_evidence import (
    EvidenceError,
    require_mapping,
    require_true_flags,
    run_evidence_cli,
    validate_schema_version,
)

DEFAULT_EVIDENCE = Path("docs/prd/rust/evidence/prd07/orchestration_hotspot_release_gates.json")
SCHEMA_VERSION = "maf.prd07.orchestration_hotspot_evidence.v1"
INVALID_CODE = "prd07_orchestration_hotspot_evidence_invalid"
PENDING_CODE = "prd07_orchestration_hotspot_evidence_pending"
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


def _as_mapping(payload: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    return require_mapping(payload, key, invalid_code=INVALID_CODE)


def _require_true(mapping: Mapping[str, Any], keys: Sequence[str], *, parent: str) -> dict[str, bool]:
    return require_true_flags(mapping, keys, parent=parent, invalid_code=INVALID_CODE)


def _validate_classification(payload: Mapping[str, Any]) -> dict[str, Any]:
    value = _as_mapping(payload, "classification")
    if value.get("conditional_candidate") is not True:
        raise EvidenceError(INVALID_CODE, "classification.conditional_candidate must be true")
    if value.get("in_mandatory_rust_target_set") is not False:
        raise EvidenceError(INVALID_CODE, "classification.in_mandatory_rust_target_set must be false")
    if value.get("implementation_prd_required") is not True:
        raise EvidenceError(INVALID_CODE, "classification.implementation_prd_required must be true")
    if value.get("candidate_crate_reserved") != "maf_orchestration_kernel":
        raise EvidenceError(INVALID_CODE, "classification.candidate_crate_reserved must be maf_orchestration_kernel")
    if value.get("current_rust_kernel_started") is not False:
        raise EvidenceError(
            INVALID_CODE,
            "classification.current_rust_kernel_started must remain false until a separate implementation PRD is approved",
        )
    if Path("native/crates/maf_orchestration_kernel").exists():
        raise EvidenceError(
            INVALID_CODE,
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
        raise EvidenceError(INVALID_CODE, f"startup_readiness.{gate} must be a JSON object")
    status = value.get("status")
    if status not in {"ready", "pending"}:
        raise EvidenceError(INVALID_CODE, f"startup_readiness.{gate}.status must be ready or pending")
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
            raise EvidenceError(INVALID_CODE, f"startup_readiness.{gate} is required")
        gate_value = value[gate]
        gate_status = _status_of(gate_value, gate=gate)
        results[gate] = dict(gate_value)  # type: ignore[arg-type]
        if gate_status != "ready":
            pending.append(gate)

    if status == READY_STATUS and pending and not allow_pending:
        raise EvidenceError(PENDING_CODE, "pending startup gates remain: " + ", ".join(pending))
    return results, pending


def _validate_baseline_tests(payload: Mapping[str, Any]) -> dict[str, list[str]]:
    value = _as_mapping(payload, "baseline_tests")
    results: dict[str, list[str]] = {}
    for section in ("python_orchestration", "token_counter", "main_agent_sanitizer"):
        paths = value.get(section)
        if not isinstance(paths, list) or not paths:
            raise EvidenceError(INVALID_CODE, f"baseline_tests.{section} must be a non-empty list")
        normalized: list[str] = []
        for item in paths:
            if not isinstance(item, str) or not item:
                raise EvidenceError(INVALID_CODE, f"baseline_tests.{section} items must be paths")
            if not Path(item).exists():
                raise EvidenceError(INVALID_CODE, f"baseline test does not exist: {item}")
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
        raise EvidenceError(INVALID_CODE, "startup_blockers must be a list of strings")
    normalized = [str(item) for item in blockers]
    if status == READY_STATUS and normalized and not allow_pending:
        raise EvidenceError(PENDING_CODE, "startup blockers remain: " + ", ".join(normalized))
    return normalized


def validate_evidence(payload: Mapping[str, Any], *, allow_pending: bool) -> dict[str, Any]:
    validate_schema_version(payload, expected=SCHEMA_VERSION, invalid_code=INVALID_CODE)
    status = str(payload.get("status") or "")
    if status not in {GUARDED_STATUS, READY_STATUS}:
        raise EvidenceError(INVALID_CODE, f"status must be {GUARDED_STATUS} or {READY_STATUS}")

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


def main() -> int:
    return run_evidence_cli(
        validate_evidence,
        default_evidence=DEFAULT_EVIDENCE,
        description="Validate PRD07 orchestration hotspot conditional-candidate evidence.",
        invalid_code=INVALID_CODE,
        missing_pending_code=PENDING_CODE,
        status_messages={
            "ready": "prd07_orchestration_hotspot_evidence_ready",
            "guarded": "prd07_orchestration_hotspot_evidence_guarded",
        },
        pending_message_prefix="prd07_orchestration_hotspot_evidence_pending",
        success_statuses=("ready", "guarded"),
        blockers_key="startup_blockers",
        allow_pending_help="Treat future startup-readiness blockers as an explicit pending status.",
        catch_runtime_error=False,
    )


if __name__ == "__main__":
    raise SystemExit(main())
