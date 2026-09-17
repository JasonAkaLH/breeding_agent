from __future__ import annotations

import inspect
import unittest
import weakref
from dataclasses import FrozenInstanceError, asdict, fields
from datetime import datetime, timezone

from src.core import contracts
from src.core import models
from src.core.errors import MessageIdentityConflictError
from src.core.models import (
    ConversationAdmissionCloseDisposition,
    ConversationAdmissionCloseRequest,
    MessageIdentityDisposition,
    MessageIdentityKind,
    MessageIdentityReservationResult,
    SubmissionAdmissionResult,
    SubmissionAdmissionDisposition,
    SubmissionAdmissionHandle,
    SubmissionAdmissionPhase,
    SubmissionAdmissionRequest,
    SubmissionAdmissionState,
    SubmissionHandoffState,
    SubmissionClaimResult,
    SubmissionAuthorityState,
    SubmissionPreparationState,
    SubmissionPreparationRecord,
    SubmissionProjectionState,
    Task,
)


EXPECTED_METHODS = (
    "admit_submission",
    "claim_pending_submission",
    "renew_submission_claim",
    "acknowledge_submission_projection",
    "prepare_submission_handoff",
    "get_submission_preparation",
    "acknowledge_submission_handoff",
    "close_conversation_admission",
    "reserve_message_identity",
)


class SubmissionAdmissionContractTest(unittest.TestCase):
    def test_port_owns_only_the_nine_approved_operations(self) -> None:
        direct = tuple(
            name
            for name, value in contracts.ConversationTaskAdmissionPort.__dict__.items()
            if inspect.iscoroutinefunction(value)
        )

        self.assertEqual(direct, EXPECTED_METHODS)
        self.assertTrue(contracts.ConversationTaskAdmissionPort._is_runtime_protocol)
        self.assertTrue(
            issubclass(contracts.StoragePort, contracts.ConversationTaskAdmissionPort)
        )

    def test_closed_dispositions_and_identity_kinds_match_the_design(self) -> None:
        self.assertEqual(
            tuple(item.value for item in SubmissionAdmissionDisposition),
            (
                "created",
                "idempotent_replay",
                "conversation_busy",
                "message_id_conflict",
                "conversation_not_available",
            ),
        )
        self.assertEqual(
            tuple(item.value for item in MessageIdentityKind),
            (
                "submission",
                "interrupt",
                "server_internal",
                "file_visible",
                "legacy_conflict_only",
            ),
        )
        self.assertEqual(
            tuple(item.value for item in MessageIdentityDisposition),
            (
                "created",
                "exact_replay",
                "conflict",
                "conversation_not_available",
            ),
        )
        self.assertEqual(
            tuple(item.value for item in ConversationAdmissionCloseDisposition),
            (
                "closed",
                "exact_replay",
                "conversation_not_available",
                "conflict",
            ),
        )

    def test_value_objects_are_immutable_and_handle_is_opaque(self) -> None:
        now = datetime(2026, 8, 26, tzinfo=timezone.utc)
        request = SubmissionAdmissionRequest(
            username="alice",
            conversation_id="conversation-1",
            message_id="message-1",
            task=Task(
                task_id="task-candidate",
                conversation_id="conversation-1",
                root_message_id="message-1",
                created_at=now,
                updated_at=now,
            ),
            idempotency_key="submission:alice:message-1",
            request_fingerprint="a" * 64,
            conversation_projection=b"{}",
            message_projection=b"{}",
            projection_sha256="b" * 64,
            continuation=b"{}",
            continuation_sha256="c" * 64,
            message_created_at=now,
            claim_owner="worker-1",
            claim_expires_at=now,
        )
        phase = SubmissionAdmissionPhase(
            admission_state=SubmissionAdmissionState.OPEN,
            projection_state=SubmissionProjectionState.PENDING,
            preparation_state=SubmissionPreparationState.PENDING,
            handoff_state=SubmissionHandoffState.PENDING,
        )
        secret = "claim-token-must-not-leak"
        handle = SubmissionAdmissionHandle()
        adapter_claims = weakref.WeakKeyDictionary({handle: secret})
        result = SubmissionAdmissionResult(
            disposition=SubmissionAdmissionDisposition.CREATED,
            conversation_id="conversation-1",
            message_id="message-1",
            task_id="task-1",
            phase=phase,
            handle=handle,
        )

        with self.assertRaises(FrozenInstanceError):
            request.username = "bob"  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            phase.handoff_state = SubmissionHandoffState.HANDED_OFF  # type: ignore[misc]
        self.assertNotIn(secret, repr(handle))
        self.assertNotIn(secret, repr(result))
        self.assertNotIn(secret, repr(asdict(result)))
        self.assertEqual(adapter_claims[handle], secret)
        self.assertFalse(hasattr(handle, "claim_token"))
        self.assertEqual(tuple(handle.__slots__), ("__weakref__",))
        self.assertEqual(request.task.task_id, "task-candidate")
        self.assertEqual(request.idempotency_key, "submission:alice:message-1")

    def test_close_identity_preparation_and_reservation_results_are_closed(self) -> None:
        now = datetime(2026, 8, 26, tzinfo=timezone.utc)
        close = ConversationAdmissionCloseRequest(
            username="alice",
            conversation_id="conversation-1",
            operation_id="close:alice:conversation-1",
            closed_at=now,
        )
        preparation = SubmissionPreparationRecord(
            conversation_id="conversation-1",
            message_id="message-1",
            task_id="task-1",
            prepared_execution=b"{}",
            prepared_execution_sha256="d" * 64,
            handoff_state=SubmissionHandoffState.HANDED_OFF,
            handoff_kind="agent_run",
            handoff_identity="agent-run:task-1",
        )

        self.assertEqual(close.operation_id, "close:alice:conversation-1")
        self.assertEqual(preparation.handoff_identity, "agent-run:task-1")
        self.assertIn("record", tuple(field.name for field in fields(SubmissionAdmissionResult)))
        self.assertEqual(
            tuple(field.name for field in fields(MessageIdentityReservationResult)),
            (
                "disposition",
                "message_id",
                "conversation_id",
                "identity_kind",
                "role",
                "message_type",
                "message_created_at",
                "task_id",
            ),
        )
        result_fields = tuple(
            field.name for field in fields(MessageIdentityReservationResult)
        )
        self.assertNotIn("fingerprint", result_fields)
        self.assertNotIn("reserved_at", result_fields)
        self.assertFalse(hasattr(models, "MessageIdentityRecord"))

    def test_claim_result_exposes_only_cursor_scoped_backlog_observability(self) -> None:
        expires_at = datetime(2026, 8, 26, tzinfo=timezone.utc)
        result = SubmissionClaimResult(
            found=False,
            authority_state=SubmissionAuthorityState.FINALIZED,
            finalization_receipt_sha256="f" * 64,
            pending_count=2,
            earliest_claim_expires_at=expires_at,
        )

        self.assertEqual(result.pending_count, 2)
        self.assertEqual(result.earliest_claim_expires_at, expires_at)
        self.assertNotIn("blocked", tuple(field.name for field in fields(result)))

    def test_message_identity_conflict_error_is_stable_and_low_sensitivity(self) -> None:
        error = MessageIdentityConflictError()

        self.assertEqual(error.code, "message_id_conflict")
        self.assertEqual(str(error), "message_id_conflict")


if __name__ == "__main__":
    unittest.main()
