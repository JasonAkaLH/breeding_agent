from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy.orm import Session

from src.core.enums import RoutingMode, TaskStatus
from src.core.errors import MessageIdentityConflictError
from src.core.models import (
    Conversation,
    SubmissionAdmissionDisposition,
    SubmissionAdmissionPhase,
    SubmissionAdmissionRequest,
    SubmissionAdmissionState,
    SubmissionHandoffState,
    SubmissionPreparationState,
    SubmissionProjectionState,
    SubmissionProjectionAcknowledgementRequest,
    SubmissionRecoveryRecord,
    Task,
)
from src.storage.sqlalchemy_models import ConversationRow, MessageRow, TaskRow
from src.storage.sqlite.repositories import SQLiteStateRepository, SQLiteStorage
from tests.storage.support import SQLiteStorageTestCase


class SubmissionAdmissionSQLiteTest(SQLiteStorageTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.storage = SQLiteStorage(
            self.session_factory,
            mcp_task_authority_mode="off",
        )

    def test_off_admission_is_atomic_and_exact_replay_returns_first_ids(self) -> None:
        first = _request()
        created = asyncio.run(self.storage.admit_submission(first))
        retry = _request(
            task_id="task-retry",
            now=first.message_created_at + timedelta(seconds=5),
        )
        replay = asyncio.run(self.storage.admit_submission(retry))

        self.assertEqual(created.disposition, SubmissionAdmissionDisposition.CREATED)
        self.assertEqual(replay.disposition, SubmissionAdmissionDisposition.IDEMPOTENT_REPLAY)
        self.assertEqual(replay.task_id, "task-1")
        self.assertEqual(replay.message_created_at, first.message_created_at.replace(tzinfo=None))
        self.assertIsNone(replay.record)
        self.assertIsNone(replay.handle)
        with self.session_factory() as session:
            row = session.get(MessageRow, "message-1")
            self.assertIsNotNone(row)
            self.assertIn("__maf_private_submission_admission_v1", row.message_metadata)
            self.assertEqual(session.query(TaskRow).count(), 1)
        loaded = asyncio.run(self.storage.get_message("message-1"))
        self.assertEqual(loaded.metadata, {"visible": "yes"})

    def test_global_message_conflict_is_checked_before_conversation_busy(self) -> None:
        asyncio.run(self.storage.admit_submission(_request()))
        conflict = asyncio.run(
            self.storage.admit_submission(
                _request(
                    conversation_id="conversation-other",
                    username="other",
                    content="changed",
                )
            )
        )
        busy = asyncio.run(
            self.storage.admit_submission(
                _request(message_id="message-2", task_id="task-2")
            )
        )

        self.assertEqual(
            conflict.disposition,
            SubmissionAdmissionDisposition.MESSAGE_ID_CONFLICT,
        )
        self.assertEqual(
            busy.disposition,
            SubmissionAdmissionDisposition.CONVERSATION_BUSY,
        )

    def test_fingerprint_change_conflicts_without_mutating_first_rows(self) -> None:
        first = _request()
        asyncio.run(self.storage.admit_submission(first))
        conflict = asyncio.run(
            self.storage.admit_submission(_request(fingerprint="b" * 64))
        )

        self.assertEqual(
            conflict.disposition,
            SubmissionAdmissionDisposition.MESSAGE_ID_CONFLICT,
        )
        loaded = asyncio.run(self.storage.get_message("message-1"))
        self.assertEqual(loaded.content, "hello")
        self.assertEqual(asyncio.run(self.storage.get_task("task-1")), first.task)

    def test_replay_survives_canonical_task_mutable_state_progress(self) -> None:
        first = _request()
        asyncio.run(self.storage.admit_submission(first))
        asyncio.run(
            self.storage.save_task(
                replace(first.task, status=TaskStatus.RUNNING, summary="progressed")
            )
        )

        replay = asyncio.run(
            self.storage.admit_submission(
                _request(
                    task_id="task-retry",
                    now=first.message_created_at + timedelta(seconds=5),
                )
            )
        )

        self.assertEqual(
            replay.disposition,
            SubmissionAdmissionDisposition.IDEMPOTENT_REPLAY,
        )
        self.assertEqual(replay.task_id, first.task.task_id)
        self.assertIsNone(replay.record)
        self.assertIsNone(replay.handle)

    def test_task_failure_rolls_back_conversation_message_and_receipt(self) -> None:
        with patch.object(
            SQLiteStateRepository,
            "save_task",
            side_effect=RuntimeError("fault"),
        ):
            with self.assertRaisesRegex(RuntimeError, "fault"):
                asyncio.run(self.storage.admit_submission(_request()))

        self.assertIsNone(asyncio.run(self.storage.get_conversation("conversation-1")))
        self.assertIsNone(asyncio.run(self.storage.get_message("message-1")))
        self.assertIsNone(asyncio.run(self.storage.get_task("task-1")))

    def test_conversation_write_failure_rolls_back_every_admission_row(self) -> None:
        original_flush = Session.flush

        def fail_conversation(session: Session, *args: object, **kwargs: object) -> None:
            if any(isinstance(row, ConversationRow) for row in session.new):
                raise RuntimeError("conversation-write-fault")
            original_flush(session, *args, **kwargs)

        with patch.object(Session, "flush", new=fail_conversation):
            with self.assertRaisesRegex(RuntimeError, "conversation-write-fault"):
                asyncio.run(self.storage.admit_submission(_request()))
        self._assert_no_admission_rows()

    def test_message_write_failure_rolls_back_conversation_and_task(self) -> None:
        original_flush = Session.flush

        def fail_message(session: Session, *args: object, **kwargs: object) -> None:
            if any(isinstance(row, MessageRow) for row in session.new):
                raise RuntimeError("message-write-fault")
            original_flush(session, *args, **kwargs)

        with patch.object(Session, "flush", new=fail_message):
            with self.assertRaisesRegex(RuntimeError, "message-write-fault"):
                asyncio.run(self.storage.admit_submission(_request()))
        self._assert_no_admission_rows()

    def test_enforce_projection_writes_conversation_and_user_message_but_no_task(self) -> None:
        request = _request()
        record = _record(request)
        with self.session_factory() as session:
            repo = SQLiteStateRepository(session, task_authority_mode="enforce")
            repo.project_submission_admission(record)
            repo.project_submission_admission(record)
            session.commit()

        self.assertIsNotNone(asyncio.run(self.storage.get_conversation("conversation-1")))
        self.assertIsNotNone(asyncio.run(self.storage.get_message("message-1")))
        self.assertIsNone(asyncio.run(self.storage.get_task("task-1")))

    def test_enforce_projection_rejects_existing_conversation_created_at_drift(self) -> None:
        request = _request()
        asyncio.run(
            self.storage.save_conversation(
                Conversation(
                    conversation_id=request.conversation_id,
                    username=request.username,
                    created_at=request.message_created_at.replace(tzinfo=None)
                    - timedelta(days=1),
                )
            )
        )
        with self.session_factory() as session:
            repo = SQLiteStateRepository(session, task_authority_mode="enforce")
            with self.assertRaisesRegex(RuntimeError, "submission_projection_conflict"):
                repo.project_submission_admission(_record(request))
        self.assertIsNone(asyncio.run(self.storage.get_message(request.message_id)))

    def test_closed_submission_envelopes_reject_wrong_schema_value_and_nested_unknown(self) -> None:
        cases = (
            _mutate_request(_request(), "conversation_projection", lambda value: value.update(schema="wrong")),
            _mutate_request(_request(), "message_projection", lambda value: value.update(stream_status="streaming")),
            _mutate_request(
                _request(),
                "continuation",
                lambda value: value["model_options"].update(unknown=True),
            ),
        )
        for request in cases:
            with self.subTest(request=request), self.assertRaises(RuntimeError):
                asyncio.run(self.storage.admit_submission(request))
        self._assert_no_admission_rows()

    def test_generic_message_updates_preserve_private_receipt_and_reject_rebinding(self) -> None:
        request = _request()
        asyncio.run(self.storage.admit_submission(request))
        message = asyncio.run(self.storage.get_message(request.message_id))
        updated = replace(
            message,
            content="streamed",
            stream_status="streaming",
            updated_at=request.message_created_at + timedelta(seconds=1),
        )
        saved = asyncio.run(self.storage.save_message(updated))

        self.assertEqual(saved.content, "streamed")
        self.assertNotIn("__maf_private_submission_admission_v1", saved.metadata)
        with self.session_factory() as session:
            row = session.get(MessageRow, request.message_id)
            self.assertIn("__maf_private_submission_admission_v1", row.message_metadata)
        with self.assertRaises(MessageIdentityConflictError):
            asyncio.run(
                self.storage.save_message(
                    replace(updated, conversation_id="conversation-other")
                )
            )

    def test_enforce_adapter_keeps_claim_opaque_and_projects_before_ack(self) -> None:
        sidecar = _FakeSubmissionSidecar()
        storage = SQLiteStorage(
            self.session_factory,
            runtime_sidecar_client=sidecar,
            mcp_task_authority_mode="enforce",
        )
        request = _request()
        admitted = asyncio.run(storage.admit_submission(request))

        self.assertNotIn("claim-secret", repr(admitted))
        self.assertIsNotNone(admitted.handle)
        phase = asyncio.run(
            storage.acknowledge_submission_projection(
                SubmissionProjectionAcknowledgementRequest(
                    handle=admitted.handle,
                    projection_sha256=request.projection_sha256,
                    acknowledged_at=request.message_created_at,
                )
            )
        )
        self.assertEqual(phase.projection_state, SubmissionProjectionState.PROJECTED)
        self.assertEqual(sidecar.ack_count, 1)
        with self.session_factory() as session:
            self.assertIsNotNone(session.get(MessageRow, request.message_id))
            self.assertIsNone(session.get(TaskRow, request.task.task_id))

    def _assert_no_admission_rows(self) -> None:
        self.assertIsNone(asyncio.run(self.storage.get_conversation("conversation-1")))
        self.assertIsNone(asyncio.run(self.storage.get_message("message-1")))
        self.assertIsNone(asyncio.run(self.storage.get_task("task-1")))


def _request(
    *,
    username: str = "owner",
    conversation_id: str = "conversation-1",
    message_id: str = "message-1",
    task_id: str = "task-1",
    content: str = "hello",
    fingerprint: str = "a" * 64,
    now: datetime | None = None,
) -> SubmissionAdmissionRequest:
    now = now or datetime(2026, 8, 26, tzinfo=timezone.utc)

    def canonical(value: object) -> bytes:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()

    conversation = canonical(
        {
            "schema": "maf.submission.conversation_projection.v1",
            "conversation_id": conversation_id,
            "username": username,
            "status": "active",
            "current_task_id": task_id,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "create_if_missing": True,
        }
    )
    message = canonical(
        {
            "schema": "maf.submission.message_projection.v1",
            "message_id": message_id,
            "conversation_id": conversation_id,
            "role": "user",
            "content": content,
            "task_id": task_id,
            "stream_status": "complete",
            "message_created_at": now.isoformat(),
            "message_type": "chat",
            "metadata": {"visible": "yes"},
            "updated_at": now.isoformat(),
        }
    )
    continuation = canonical(
        {
            "schema": "maf.submission.continuation.v1",
            "request_fingerprint": fingerprint,
            "conversation_id": conversation_id,
            "message_id": message_id,
            "task_id": task_id,
            "owner_scope": username,
            "message_content_sha256": hashlib.sha256(content.encode()).hexdigest(),
            "routing_mode": "auto",
            "requested_capability_id": None,
            "model_options": {
                "model_edition": None,
                "reasoning_effort": "medium",
                "thinking_enabled": False,
            },
            "bundle_revisions": {
                "skill_bundle_revision": None,
                "mcp_bundle_revision": None,
            },
            "execution_metadata": {
                "requested_capability_alias": None,
                "canonical_capability_id": None,
                "mcp_dispatch_server_id": None,
                "mcp_binding_mode": None,
                "mcp_command": None,
                "mcp_execution_mode": None,
                "mcp_rollout_config_version": None,
                "mcp_route_reason_code": None,
                "mcp_rollout_mode": None,
                "defer_task_completed_until_pending_skill_context_processed": None,
                "forced_by_mcp_command": None,
                "mcp_shadow_enabled": None,
            },
            "upload_refs": [],
            "sheet_selections": {},
            "mcp_binding": None,
            "mcp_assignment": None,
            "available_mcp_servers": [],
            "pending_context": None,
            "initial_no_server_eligible": False,
        }
    )
    return SubmissionAdmissionRequest(
        username=username,
        conversation_id=conversation_id,
        message_id=message_id,
        task=Task(
            task_id=task_id,
            conversation_id=conversation_id,
            root_message_id=message_id,
            status=TaskStatus.ACCEPTED,
            routing_mode=RoutingMode.AUTO,
            summary=content,
            created_at=now.replace(tzinfo=None),
            updated_at=now.replace(tzinfo=None),
        ),
        idempotency_key=f"submission:{username}:{message_id}",
        request_fingerprint=fingerprint,
        conversation_projection=conversation,
        message_projection=message,
        projection_sha256=hashlib.sha256(
            b"maf.submission.projection.v1\0" + conversation + b"\0" + message
        ).hexdigest(),
        continuation=continuation,
        continuation_sha256=hashlib.sha256(
            b"maf.submission.continuation.v1\0" + continuation
        ).hexdigest(),
        message_created_at=now,
        claim_owner="worker",
        claim_expires_at=now + timedelta(seconds=30),
    )


def _record(request: SubmissionAdmissionRequest) -> SubmissionRecoveryRecord:
    return SubmissionRecoveryRecord(
        username=request.username,
        conversation_id=request.conversation_id,
        message_id=request.message_id,
        task_id=request.task.task_id,
        conversation_projection=request.conversation_projection,
        message_projection=request.message_projection,
        projection_sha256=request.projection_sha256,
        continuation=request.continuation,
        continuation_sha256=request.continuation_sha256,
        prepared_execution=None,
        prepared_execution_sha256=None,
        phase=SubmissionAdmissionPhase(
            admission_state=SubmissionAdmissionState.OPEN,
            projection_state=SubmissionProjectionState.PENDING,
            preparation_state=SubmissionPreparationState.PENDING,
            handoff_state=SubmissionHandoffState.PENDING,
        ),
        created_at=request.message_created_at,
    )


def _mutate_request(
    request: SubmissionAdmissionRequest,
    field: str,
    mutate: Callable[[dict[str, object]], None],
) -> SubmissionAdmissionRequest:
    value = json.loads(getattr(request, field))
    mutate(value)
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    if field == "continuation":
        return replace(
            request,
            continuation=payload,
            continuation_sha256=hashlib.sha256(
                b"maf.submission.continuation.v1\0" + payload
            ).hexdigest(),
        )
    conversation = (
        payload if field == "conversation_projection" else request.conversation_projection
    )
    message = payload if field == "message_projection" else request.message_projection
    return replace(
        request,
        **{field: payload},
        projection_sha256=hashlib.sha256(
            b"maf.submission.projection.v1\0" + conversation + b"\0" + message
        ).hexdigest(),
    )


class _FakeSubmissionSidecar:
    def __init__(self) -> None:
        self.admission: dict[str, object] | None = None
        self.ack_count = 0

    def admit_submission(self, **request: object) -> dict[str, object]:
        task = dict(request["task"])
        self.admission = {
            "message_id": request["message_id"],
            "task_id": request["task_id"],
            "conversation_id": request["conversation_id"],
            "username": request["username"],
            "request_fingerprint": request["request_fingerprint"],
            "conversation_projection_json": request["conversation_projection_json"],
            "message_projection_json": request["message_projection_json"],
            "projection_sha256": request["projection_sha256"],
            "continuation_json": request["continuation_json"],
            "continuation_sha256": request["continuation_sha256"],
            "projection_state": "pending",
            "preparation_state": "pending",
            "prepared_execution_json": None,
            "prepared_execution_sha256": None,
            "handoff_state": "pending",
            "handoff_kind": None,
            "handoff_identity": None,
            "created_at_ms": request["now_ms"],
            "updated_at_ms": request["now_ms"],
            "closed": False,
            "task": task,
            "idempotency_key": request["idempotency_key"],
        }
        return {
            "operation": "submission_admit",
            "disposition": "created",
            "admission": self.admission,
            "claim": {
                "owner": request["workflow_owner"],
                "token": "claim-secret",
                "expires_at_ms": int(request["now_ms"])
                + int(request["claim_ttl_ms"]),
            },
            "error": None,
        }

    def acknowledge_submission_projection(
        self, **_request: object
    ) -> dict[str, object]:
        self.ack_count += 1
        assert self.admission is not None
        self.admission = {**self.admission, "projection_state": "projected"}
        return {
            "operation": "submission_projection_acknowledge",
            "admission": self.admission,
            "duplicate": False,
            "error": None,
        }
