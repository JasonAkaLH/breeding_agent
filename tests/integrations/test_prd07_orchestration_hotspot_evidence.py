from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from scripts.validate_prd07_orchestration_hotspot_evidence import validate_evidence


SCRIPT = Path("scripts/validate_prd07_orchestration_hotspot_evidence.py")


class PRD07OrchestrationHotspotEvidenceTest(unittest.TestCase):
    def test_current_candidate_guard_ledger_validates_without_starting_rust_kernel(self) -> None:
        result = subprocess.run(
            [sys.executable, "-S", str(SCRIPT), "--json"],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "guarded")
        self.assertEqual(payload["pending_gates"], [])
        self.assertIn("startup_blockers", payload)
        self.assertIn("performance_or_reliability_evidence_missing", payload["startup_blockers"])

    def test_candidate_start_requires_all_upgrade_gates(self) -> None:
        payload = _candidate_payload()
        payload["startup_readiness"]["performance_or_reliability_evidence"] = {"status": "pending"}

        result = validate_evidence(payload, allow_pending=True)
        self.assertEqual(result["status"], "pending")
        self.assertIn("performance_or_reliability_evidence", result["pending_gates"])

        with self.assertRaisesRegex(RuntimeError, "prd07_orchestration_hotspot_evidence_pending"):
            validate_evidence(payload, allow_pending=False)

    def test_complete_synthetic_start_ready_evidence_passes_strict_validation(self) -> None:
        payload = _candidate_payload()

        result = validate_evidence(payload, allow_pending=False)

        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["pending_gates"], [])
        self.assertEqual(result["results"]["classification"]["candidate_crate_reserved"], "maf_orchestration_kernel")

    def test_cli_strict_candidate_ready_evidence_reports_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            evidence = Path(temp_dir) / "evidence.json"
            evidence.write_text(json.dumps(_candidate_payload()), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--evidence", str(evidence)],
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("prd07_orchestration_hotspot_evidence_ready", result.stdout)


def _candidate_payload() -> dict[str, Any]:
    return {
        "schema_version": "maf.prd07.orchestration_hotspot_evidence.v1",
        "status": "candidate_ready_to_start",
        "last_updated": "2026-05-18",
        "classification": {
            "conditional_candidate": True,
            "in_mandatory_rust_target_set": False,
            "implementation_prd_required": True,
            "separate_implementation_prd": "docs/prd/rust/08-OrchestrationHotspotImplementationPRD.md",
            "candidate_crate_reserved": "maf_orchestration_kernel",
            "current_rust_kernel_started": False,
        },
        "scope_boundary": {
            "allowed_rust_scope": {
                "dag_validator": True,
                "scheduler_policy": True,
                "completion_policy": True,
                "backpressure": True,
                "payload_policy": True,
                "token_budget_kernel": True,
                "artifact_dependency_sanitizer": True,
                "large_payload_parser": True,
                "optional_frontend_wasm_parser": True,
            },
            "excluded_from_rust_scope": {
                "llm_planner_prompt": True,
                "provider_fallback": True,
                "router_glue": True,
                "product_answer_strategy": True,
                "react_or_ant_design_ui": True,
            },
        },
        "startup_readiness": {
            "core_lifecycle_stable": {"status": "ready", "evidence": "PRD02 contract stable"},
            "store_event_shadow_compare_stable": {"status": "ready", "evidence": "PRD03 shadow compare stable"},
            "skill_mcp_boundaries_stable": {"status": "ready", "evidence": "PRD04/05 boundaries stable"},
            "performance_or_reliability_evidence": {"status": "ready", "evidence": "benchmark issue #123"},
            "python_baseline_defined": {"status": "ready", "evidence": "tests/orchestration golden suite"},
            "candidate_baseline_plan_defined": {"status": "ready", "evidence": "implementation PRD"},
            "shadow_compare_plan_defined": {"status": "ready", "evidence": "implementation PRD"},
            "supply_chain_gates_defined": {"status": "ready", "evidence": "PRD01 inherited gates"},
            "benchmark_slo_gates_defined": {"status": "ready", "evidence": "implementation PRD"},
            "migration_dr_runbook_defined": {"status": "ready", "evidence": "implementation PRD"},
            "ops_runbook_defined": {"status": "ready", "evidence": "implementation PRD"},
            "legacy_decommission_plan_defined": {"status": "ready", "evidence": "implementation PRD"},
        },
        "baseline_tests": {
            "python_orchestration": [
                "tests/orchestration/test_agent_loop.py",
                "tests/orchestration/test_agent_invocation.py",
                "tests/orchestration/test_agent_final_output.py",
                "tests/orchestration/test_backpressure.py",
            ],
            "token_counter": ["tests/integrations/test_token_counter.py"],
            "main_agent_sanitizer": ["tests/capabilities/main_agent/test_conversation_memory_prompt.py"],
        },
        "future_release_gates": {
            "artifact_provenance_required": True,
            "sbom_required": True,
            "allowlist_required": True,
            "python_js_baseline_required": True,
            "rust_wasm_candidate_baseline_required": True,
            "ffi_or_wasm_overhead_required": True,
            "p50_p95_p99_cpu_memory_payload_required": True,
            "state_migration_lock_backup_restore_required": True,
            "dashboard_alert_slo_runbook_drill_required": True,
            "legacy_duplicate_semantics_decommission_required": True,
        },
        "startup_blockers": [],
    }


if __name__ == "__main__":
    unittest.main()
