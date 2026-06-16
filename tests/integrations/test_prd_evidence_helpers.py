from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.prd_evidence import (
    EvidenceError,
    allowed_digest_sets,
    collect_gate_results,
    finish_release_gate_result,
    load_json_object,
    required_mapping,
)


class PrdEvidenceHelpersTest(unittest.TestCase):
    def test_load_json_object_rejects_non_object_with_caller_code(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "evidence.json"
            path.write_text("[]", encoding="utf-8")

            with self.assertRaises(EvidenceError) as caught:
                load_json_object(path, invalid_code="prd_test_invalid")

        self.assertEqual(caught.exception.code, "prd_test_invalid")
        self.assertIn("must contain a JSON object", str(caught.exception))

    def test_required_mapping_distinguishes_pending_from_invalid_payloads(self) -> None:
        with self.assertRaises(EvidenceError) as pending:
            required_mapping(
                {"gate": {}},
                "gate",
                lambda value: dict(value),
                pending_code="prd_test_pending",
                invalid_code="prd_test_invalid",
            )
        self.assertEqual(pending.exception.code, "prd_test_pending")

        with self.assertRaises(EvidenceError) as invalid:
            required_mapping(
                {"gate": []},
                "gate",
                lambda value: dict(value),
                pending_code="prd_test_pending",
                invalid_code="prd_test_invalid",
            )
        self.assertEqual(invalid.exception.code, "prd_test_invalid")

    def test_collect_gate_results_preserves_pending_reason_when_allowed(self) -> None:
        def ready() -> dict[str, str]:
            return {"status": "ready"}

        def pending() -> dict[str, str]:
            raise EvidenceError("prd_test_pending", "external evidence is pending")

        results, pending_gates = collect_gate_results(
            (("ready_gate", ready), ("pending_gate", pending)),
            allow_pending=True,
            pending_code="prd_test_pending",
        )

        self.assertEqual(pending_gates, ["pending_gate"])
        self.assertEqual(results["ready_gate"], {"status": "ready"})
        self.assertIn("prd_test_pending", results["pending_gate"]["reason"])

    def test_finish_release_gate_result_fails_closed_on_blockers_without_allow_pending(self) -> None:
        payload = {"blockers": ["deployment allowlist missing"]}

        with self.assertRaises(EvidenceError) as strict:
            finish_release_gate_result(
                payload,
                results={},
                pending=[],
                allow_pending=False,
                pending_code="prd_test_pending",
            )
        self.assertEqual(strict.exception.code, "prd_test_pending")

        allowed = finish_release_gate_result(
            payload,
            results={},
            pending=[],
            allow_pending=True,
            pending_code="prd_test_pending",
        )
        self.assertEqual(allowed["status"], "pending")
        self.assertEqual(allowed["blockers"], ["deployment allowlist missing"])

    def test_allowed_digest_sets_require_lists_and_normalize_values(self) -> None:
        allowed_checksums, allowed_locks = allowed_digest_sets(
            {
                "allowed_artifact_checksums": ["sha256:wheel", 7],
                "allowed_cargo_lock_digests": ["sha256:cargo-lock"],
            },
            invalid_code="prd_test_invalid",
        )

        self.assertEqual(allowed_checksums, {"sha256:wheel", "7"})
        self.assertEqual(allowed_locks, {"sha256:cargo-lock"})
        with self.assertRaises(EvidenceError) as invalid:
            allowed_digest_sets(
                {"allowed_artifact_checksums": "sha256:wheel", "allowed_cargo_lock_digests": []},
                invalid_code="prd_test_invalid",
            )
        self.assertEqual(invalid.exception.code, "prd_test_invalid")


if __name__ == "__main__":
    unittest.main()
