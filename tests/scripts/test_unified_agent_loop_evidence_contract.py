from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.validate_unified_agent_loop_evidence import (
    EvidenceContractError,
    collect_active_prd_matches,
    collect_legacy_test_matches,
    validate_handoff_schedule,
    validate_phase_evidence,
)


_REPO_ROOT = Path(__file__).resolve().parents[2]


class UnifiedAgentLoopEvidenceContractTest(unittest.TestCase):
    def test_phase_six_repository_inventory_and_cutover_handoff_are_closed(self) -> None:
        result = validate_phase_evidence(_REPO_ROOT, phase=6, require_closed=True)

        self.assertEqual(result["status"], "closed")
        self.assertEqual(result["active_prd_count"], 26)
        self.assertEqual(result["legacy_test_count"], 55)
        self.assertEqual(result["execution_entry_count"], 9)
        self.assertEqual(
            result["handoffs"],
            {
                "active-prd-inventory.md": "closed",
                "cutover-readiness.md": "closed",
                "dag-runtime-deletion-report.md": "closed",
                "destructive-migration-evidence.md": "open",
            },
        )

    def test_collectors_exclude_new_authority_and_require_exact_test_scope(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            old_doc = root / "docs" / "prd" / "backend" / "old.md"
            new_doc = (
                root
                / "docs"
                / "prd"
                / "backend"
                / "unified-agent-loop"
                / "README.md"
            )
            matched_test = root / "tests" / "test_old_runtime.py"
            unrelated_test = root / "tests" / "test_unrelated.py"
            for path, text in (
                (old_doc, "WorkflowPlan and max_replans"),
                (new_doc, "WorkflowPlan and RuntimeReplanner"),
                (matched_test, "TaskEdge"),
                (unrelated_test, "ordinary behavior"),
            ):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")

            self.assertEqual(
                collect_active_prd_matches(root),
                {"docs/prd/backend/old.md": ("WorkflowPlan", "max_replans")},
            )
            self.assertEqual(
                collect_legacy_test_matches(root),
                {"tests/test_old_runtime.py": ("TaskEdge",)},
            )

    def test_phase_seven_evidence_is_present_but_not_finally_closed(self) -> None:
        self.assertEqual(
            validate_handoff_schedule(
                _REPO_ROOT,
                phase=7,
                require_closed=False,
            )["destructive-migration-evidence.md"],
            "open",
        )
        with self.assertRaisesRegex(
            EvidenceContractError,
            "destructive-migration-evidence.md evidence status must be closed",
        ):
            validate_handoff_schedule(
                _REPO_ROOT,
                phase=7,
                require_closed=True,
            )

    def test_handoff_becomes_required_at_owner_phase(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            handoff_dir = root / "docs" / "prd" / "backend" / "unified-agent-loop"
            handoff_dir.mkdir(parents=True)

            self.assertEqual(
                validate_handoff_schedule(root, phase=4, require_closed=True)[
                    "cutover-readiness.md"
                ],
                "not_due",
            )
            with self.assertRaisesRegex(
                EvidenceContractError, "cutover-readiness.md is required in phase 5"
            ):
                validate_handoff_schedule(root, phase=5, require_closed=True)

            (handoff_dir / "cutover-readiness.md").write_text(
                "# readiness\n\n"
                "- **证据状态**：closed\n\n"
                "commit start resume cancel recovery blocker schema\n",
                encoding="utf-8",
            )
            self.assertEqual(
                validate_handoff_schedule(root, phase=5, require_closed=True)[
                    "cutover-readiness.md"
                ],
                "closed",
            )


if __name__ == "__main__":
    unittest.main()
