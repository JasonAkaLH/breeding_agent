from __future__ import annotations

import unittest
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class _BoundaryProof:
    boundary: int
    injection_point: str
    authority_before_restart: str
    recovery_projection: str
    expected_network_delta: int
    proof_test: str


BOUNDARY_PROOFS = (
    _BoundaryProof(
        1,
        "intent_armed_before_outbox",
        "intent_without_claimed_network_call",
        "pending_or_safe_failure",
        0,
        "tests.storage.test_user_mcp_no_server_intent.UserMCPNoServerIntentTest.test_expired_dispatch_resume_claim_is_reclaimed_without_call_replay",
    ),
    _BoundaryProof(
        2,
        "payload_fsync_before_action_transaction",
        "orphan_encrypted_payload",
        "retained_then_deleted_after_24h",
        0,
        "tests.integrations.mcp.test_pending_action_payloads.PendingActionPayloadTest.test_orphan_cleanup_requires_absence_from_action_authority_and_24_hours",
    ),
    _BoundaryProof(
        3,
        "approval_suspend_commit_before_response",
        "one_action_one_interrupt",
        "same_waiting_approval",
        0,
        "tests.storage.test_mcp_dispatch_aggregate_repository.MCPDispatchAggregateRepositoryTest.test_suspend_and_allow_once_commit_action_interrupt_answer_and_cursor",
    ),
    _BoundaryProof(
        4,
        "approval_answer_concurrent_commit",
        "waiting_action_and_open_interrupt",
        "one_answer_one_grant_one_cursor",
        0,
        "tests.storage.test_mcp_dispatch_aggregate_repository.MCPDispatchAggregateRepositoryTest.test_always_allow_creates_grant_and_double_answer_is_single_winner",
    ),
    _BoundaryProof(
        5,
        "admission_commit_before_network_gate",
        "may_have_dispatched_call",
        "unknown_no_replay",
        0,
        "tests.storage.test_mcp_dispatch_aggregate_repository.MCPDispatchAggregateRepositoryTest.test_admission_atomically_consumes_action_and_opens_network_gate",
    ),
    _BoundaryProof(
        6,
        "transport_write_before_response",
        "may_have_dispatched_without_receipt",
        "unknown_no_replay",
        1,
        "tests.integrations.mcp.test_dispatch_coordinator.UserMCPDispatchCoordinatorTest.test_post_dispatch_transport_failure_converges_to_unknown",
    ),
    _BoundaryProof(
        7,
        "durable_result_fsync_before_candidate",
        "durable_result_without_candidate",
        "unknown_no_replay_result_retained",
        1,
        "tests.api.test_user_mcp_recovery_startup.UserMCPRecoveryStartupTest.test_candidate_seal_failure_converges_admitted_call_immediately",
    ),
    _BoundaryProof(
        8,
        "candidate_fsync_before_receipt",
        "sealed_candidate_without_receipt",
        "same_candidate_receipt",
        0,
        "tests.storage.test_mcp_dispatch_aggregate_repository.MCPDispatchAggregateRepositoryTest.test_startup_recovers_sealed_candidate_after_expired_dispatch_claim",
    ),
    _BoundaryProof(
        9,
        "receipt_commit_before_response",
        "receipt_and_resume_cursor",
        "already_committed",
        0,
        "tests.storage.test_mcp_dispatch_aggregate_repository.MCPDispatchAggregateRepositoryTest.test_completed_terminal_commit_keeps_dispatch_active_until_finalizer",
    ),
    _BoundaryProof(
        10,
        "ordinary_terminal_before_finalizer",
        "completed_call_active_dispatch",
        "one_finalizer",
        0,
        "tests.storage.test_mcp_dispatch_aggregate_repository.MCPDispatchAggregateRepositoryTest.test_two_completed_calls_keep_dispatch_active_until_one_finalizer",
    ),
    _BoundaryProof(
        11,
        "mrtr_evidence_fsync_before_adoption",
        "sealed_mrtr_evidence",
        "same_interrupt_waiting_input",
        0,
        "tests.storage.test_mcp_dispatch_aggregate_repository.MCPDispatchAggregateRepositoryTest.test_mrtr_suspend_and_answer_commit_call_interrupt_and_cursor_atomically",
    ),
    _BoundaryProof(
        12,
        "mrtr_answer_before_continuation_admission",
        "accepted_answer_and_mrtr_cursor",
        "exact_continuation",
        1,
        "tests.api.test_user_mcp_recovery_startup.UserMCPRecoveryStartupTest.test_mrtr_answer_resumes_original_action_without_selector_or_reapproval",
    ),
    _BoundaryProof(
        13,
        "remote_binding_before_adoption",
        "unpublished_remote_binding",
        "adopt_and_poll_only",
        0,
        "tests.api.test_user_mcp_recovery_startup.UserMCPRecoveryStartupTest.test_remote_task_response_is_adopted_before_dispatch_returns",
    ),
    _BoundaryProof(
        14,
        "remote_candidate_before_cursor_commit",
        "remote_terminal_candidate",
        "remote_terminal_cursor",
        0,
        "tests.api.test_user_mcp_recovery_startup.UserMCPRecoveryStartupTest.test_remote_worker_persists_terminal_remote_task_metrics",
    ),
    _BoundaryProof(
        15,
        "candidate_archiving_between_file_moves",
        "archiving_marker_partial_active",
        "archived_exact_triple",
        0,
        "tests.integrations.mcp.test_cp7_terminal_lifecycle.CP7TerminalCandidateLifecycleTest.test_startup_repairs_partial_archive_before_strict_enumeration",
    ),
    _BoundaryProof(
        16,
        "candidate_deleting_between_unlinks",
        "deleting_marker_partial_archive",
        "deleted_candidate",
        0,
        "tests.integrations.mcp.test_cp7_terminal_lifecycle.CP7TerminalCandidateLifecycleTest.test_startup_repairs_partial_archive_delete",
    ),
    _BoundaryProof(
        17,
        "result_deleting_between_manifest_and_data",
        "deleting_marker_orphan_data",
        "deleted_result",
        0,
        "tests.integrations.mcp.test_durable_result_lifecycle.DurableResultLifecycleTest.test_startup_repairs_manifest_first_partial_delete",
    ),
)


class MCPDispatchFaultInjectionMatrixTest(unittest.TestCase):
    def test_matrix_has_exactly_seventeen_closed_boundaries(self) -> None:
        self.assertEqual(tuple(item.boundary for item in BOUNDARY_PROOFS), tuple(range(1, 18)))
        self.assertTrue(
            all(item.expected_network_delta in {0, 1} for item in BOUNDARY_PROOFS)
        )
        self.assertEqual(len({item.injection_point for item in BOUNDARY_PROOFS}), 17)
        self.assertEqual(len({item.proof_test for item in BOUNDARY_PROOFS}), 17)

    def test_every_boundary_executes_its_durable_proof(self) -> None:
        failures: list[str] = []
        loader = unittest.defaultTestLoader
        for proof in BOUNDARY_PROOFS:
            result = unittest.TestResult()
            loader.loadTestsFromName(proof.proof_test).run(result)
            if not result.wasSuccessful():
                details = [
                    f"{case.id()}: {error}"
                    for case, error in (*result.failures, *result.errors)
                ]
                failures.append(
                    f"boundary {proof.boundary} ({proof.injection_point}): "
                    + " | ".join(details)
                )
        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()
