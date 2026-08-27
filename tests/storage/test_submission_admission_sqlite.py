from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy.orm import Session

from src.core.enums import ConversationStatus, RoutingMode, TaskStatus
from src.core.errors import MessageIdentityConflictError
from src.core.models import (
    Conversation,
    SubmissionAdmissionDisposition,
    SubmissionAdmissionPhase,
    SubmissionAdmissionRequest,
    SubmissionAdmissionState,
    SubmissionClaimRequest,
    SubmissionHandoffState,
    SubmissionHandoffAcknowledgementRequest,
    SubmissionPreparationLookup,
    SubmissionPreparationRequest,
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
    def test_shadow_sidecar_unavailable_does_not_change_sql_admission(self) -> None:
        class UnavailableSidecar:
            def admit_submission(self, **_request: object) -> dict[str, object]:
                raise AssertionError("shadow admission must not call Sidecar")

        storage = SQLiteStorage(
            self.session_factory,
            runtime_sidecar_client=UnavailableSidecar(),
            runtime_sidecar_shadow_sink=lambda _payload: None,
            mcp_task_authority_mode="shadow",
        )

        request = _request()
        admitted = asyncio.run(storage.admit_submission(request))
        replay = asyncio.run(
            storage.admit_submission(
                _request(
                    task_id="task-shadow-retry",
                    now=request.message_created_at + timedelta(seconds=5),
                )
            )
        )

        self.assertEqual(
            admitted.disposition,
            SubmissionAdmissionDisposition.CREATED,
        )
        self.assertEqual(
            replay.disposition,
            SubmissionAdmissionDisposition.IDEMPOTENT_REPLAY,
        )
        self.assertEqual(replay.task_id, request.task.task_id)
        self.assertIsNotNone(replay.record)
        self.assertIsNotNone(replay.handle)
        with self.session_factory() as session:
            self.assertIsNotNone(session.get(ConversationRow, "conversation-1"))
            self.assertIsNotNone(session.get(MessageRow, "message-1"))
            self.assertIsNotNone(session.get(TaskRow, "task-1"))

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
        self.assertIsNotNone(replay.record)
        self.assertIsNotNone(replay.handle)
        assert replay.record is not None
        self.assertEqual(replay.record.message_id, first.message_id)
        self.assertEqual(replay.record.task_id, first.task.task_id)
        self.assertEqual(replay.record.created_at, first.task.created_at)
        replay_projection = json.loads(replay.record.message_projection)
        replay_continuation = json.loads(replay.record.continuation)
        self.assertEqual(replay_projection["message_id"], first.message_id)
        self.assertEqual(replay_projection["task_id"], first.task.task_id)
        self.assertEqual(
            datetime.fromisoformat(
                replay_projection["message_created_at"].replace("Z", "+00:00")
            ).replace(tzinfo=None),
            first.message_created_at.replace(tzinfo=None),
        )
        self.assertEqual(replay_continuation["task_id"], first.task.task_id)
        with self.session_factory() as session:
            row = session.get(MessageRow, "message-1")
            self.assertIsNotNone(row)
            self.assertEqual(
                set(row.message_metadata["__maf_private_submission_admission_v1"]),
                {"schema", "request_fingerprint", "idempotency_key"},
            )
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

    def test_off_admission_reclaims_terminal_pointer_in_same_transaction(self) -> None:
        first = _request()
        asyncio.run(self.storage.admit_submission(first))
        asyncio.run(
            self.storage.save_task(
                replace(first.task, status=TaskStatus.COMPLETED)
            )
        )
        second = _request(
            message_id="message-2",
            task_id="task-2",
            now=first.message_created_at + timedelta(seconds=1),
        )

        admitted = asyncio.run(self.storage.admit_submission(second))

        self.assertEqual(
            admitted.disposition,
            SubmissionAdmissionDisposition.CREATED,
        )
        conversation = asyncio.run(
            self.storage.get_conversation(first.conversation_id)
        )
        self.assertEqual(conversation.current_task_id, second.task.task_id)

    def test_off_admission_keeps_missing_and_foreign_task_pointers_busy(self) -> None:
        now = datetime(2026, 8, 26, tzinfo=timezone.utc)
        for pointer_kind in ("missing", "foreign"):
            with self.subTest(pointer_kind=pointer_kind):
                conversation_id = f"conversation-{pointer_kind}"
                pointer_id = f"task-{pointer_kind}"
                asyncio.run(
                    self.storage.save_conversation(
                        Conversation(
                            conversation_id=conversation_id,
                            username="owner",
                            current_task_id=pointer_id,
                            created_at=now.replace(tzinfo=None),
                            updated_at=now.replace(tzinfo=None),
                        )
                    )
                )
                if pointer_kind == "foreign":
                    asyncio.run(
                        self.storage.save_task(
                            Task(
                                task_id=pointer_id,
                                conversation_id="different-conversation",
                                root_message_id="foreign-message",
                                status=TaskStatus.COMPLETED,
                            )
                        )
                    )

                admitted = asyncio.run(
                    self.storage.admit_submission(
                        _request(
                            conversation_id=conversation_id,
                            message_id=f"message-{pointer_kind}-new",
                            task_id=f"task-{pointer_kind}-new",
                            now=now,
                        )
                    )
                )

                self.assertEqual(
                    admitted.disposition,
                    SubmissionAdmissionDisposition.CONVERSATION_BUSY,
                )
                conversation = asyncio.run(
                    self.storage.get_conversation(conversation_id)
                )
                self.assertEqual(conversation.current_task_id, pointer_id)

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
        self.assertIsNotNone(replay.record)
        self.assertIsNotNone(replay.handle)

    def test_terminal_replay_returns_canonical_ids_without_rebuilding_handoff(self) -> None:
        first = _request()
        asyncio.run(self.storage.admit_submission(first))
        asyncio.run(
            self.storage.save_task(
                replace(first.task, status=TaskStatus.COMPLETED, summary="done")
            )
        )

        replay = asyncio.run(
            self.storage.admit_submission(
                _request(
                    task_id="task-terminal-retry",
                    now=first.message_created_at + timedelta(seconds=5),
                )
            )
        )

        self.assertEqual(
            replay.disposition,
            SubmissionAdmissionDisposition.IDEMPOTENT_REPLAY,
        )
        self.assertEqual(replay.message_id, first.message_id)
        self.assertEqual(replay.task_id, first.task.task_id)
        self.assertIsNone(replay.record)
        self.assertIsNone(replay.handle)

    def test_sql_prepared_handoff_is_private_immutable_and_restart_readable(self) -> None:
        request = _request()
        admitted = asyncio.run(self.storage.admit_submission(request))
        self.assertIsNotNone(admitted.handle)
        prepared = _legacy_prepared(request)
        prepared_sha = hashlib.sha256(
            b"maf.submission.prepared_execution.v1\0" + prepared
        ).hexdigest()
        preparation = asyncio.run(
            self.storage.prepare_submission_handoff(
                SubmissionPreparationRequest(
                    handle=admitted.handle,
                    prepared_execution=prepared,
                    prepared_execution_sha256=prepared_sha,
                    prepared_at=request.message_created_at,
                )
            )
        )
        conflicting_value = json.loads(prepared)
        conflicting_value["execution_text_sha256"] = "e" * 64
        conflicting = json.dumps(
            conflicting_value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        with self.assertRaisesRegex(RuntimeError, "submission_preparation_conflict"):
            asyncio.run(
                self.storage.prepare_submission_handoff(
                    SubmissionPreparationRequest(
                        handle=admitted.handle,
                        prepared_execution=conflicting,
                        prepared_execution_sha256=hashlib.sha256(
                            b"maf.submission.prepared_execution.v1\0" + conflicting
                        ).hexdigest(),
                        prepared_at=request.message_created_at,
                    )
                )
            )
        asyncio.run(
            self.storage.acknowledge_submission_handoff(
                SubmissionHandoffAcknowledgementRequest(
                    handle=admitted.handle,
                    prepared_execution_sha256=prepared_sha,
                    handoff_kind="agent_run",
                    handoff_identity=f"agent-run:{request.task.task_id}",
                    acknowledged_at=request.message_created_at,
                )
            )
        )
        loaded = asyncio.run(
            self.storage.get_submission_preparation(
                SubmissionPreparationLookup(
                    username=request.username,
                    conversation_id=request.conversation_id,
                    task_id=request.task.task_id,
                )
            )
        )

        self.assertEqual(loaded.prepared_execution, prepared)
        self.assertEqual(loaded.prepared_execution_sha256, prepared_sha)
        self.assertEqual(loaded.handoff_state, SubmissionHandoffState.HANDED_OFF)
        self.assertEqual(loaded.handoff_kind, "agent_run")
        self.assertEqual(loaded.handoff_identity, f"agent-run:{request.task.task_id}")
        self.assertEqual(preparation.prepared_execution, prepared)
        message = asyncio.run(self.storage.get_message(request.message_id))
        self.assertNotIn("__maf_private_submission_handoff_v1", message.metadata)
        updated = asyncio.run(
            self.storage.save_message(replace(message, content="streamed"))
        )
        self.assertNotIn("__maf_private_submission_handoff_v1", updated.metadata)
        with self.assertRaisesRegex(ValueError, "message_private_metadata_reserved"):
            asyncio.run(
                self.storage.save_message(
                    replace(
                        message,
                        metadata={"__maf_private_submission_handoff_v1": {}},
                    )
                )
            )
        with self.session_factory() as session:
            row = session.get(MessageRow, request.message_id)
            self.assertEqual(
                set(row.message_metadata["__maf_private_submission_admission_v1"]),
                {"schema", "request_fingerprint", "idempotency_key"},
            )
            private = row.message_metadata["__maf_private_submission_handoff_v1"]
            self.assertEqual(
                set(private),
                {
                    "schema",
                    "prepared_execution",
                    "prepared_execution_sha256",
                    "handoff_state",
                    "handoff_kind",
                    "handoff_identity",
                },
            )


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

    def test_enforce_projection_replaces_stale_sql_pointer_and_preserves_metadata(self) -> None:
        request = _request()
        created_at = request.message_created_at.replace(tzinfo=None)
        asyncio.run(
            self.storage.save_conversation(
                Conversation(
                    conversation_id=request.conversation_id,
                    username=request.username,
                    current_task_id="stale-sql-task",
                    title="keep-title",
                    created_at=created_at,
                    updated_at=created_at - timedelta(seconds=1),
                )
            )
        )

        with self.session_factory() as session:
            repo = SQLiteStateRepository(session, task_authority_mode="enforce")
            repo.project_submission_admission(_record(request))
            session.commit()

        conversation = asyncio.run(
            self.storage.get_conversation(request.conversation_id)
        )
        self.assertEqual(conversation.current_task_id, request.task.task_id)
        self.assertEqual(conversation.username, request.username)
        self.assertEqual(str(conversation.status), "active")
        self.assertEqual(conversation.title, "keep-title")

    def test_enforce_projection_does_not_replace_owner_or_deleting_rows(self) -> None:
        now = datetime(2026, 8, 26, tzinfo=timezone.utc)
        for case in ("owner", "deleting"):
            with self.subTest(case=case):
                request = _request(
                    conversation_id=f"conversation-enforce-{case}",
                    message_id=f"message-enforce-{case}",
                    task_id=f"task-enforce-{case}",
                    now=now,
                )
                existing = Conversation(
                    conversation_id=request.conversation_id,
                    username="other" if case == "owner" else request.username,
                    status=(
                        ConversationStatus.DELETING
                        if case == "deleting"
                        else ConversationStatus.ACTIVE
                    ),
                    current_task_id="stale-sql-task",
                    title="keep-title",
                    created_at=now.replace(tzinfo=None),
                    updated_at=now.replace(tzinfo=None),
                )
                asyncio.run(self.storage.save_conversation(existing))

                with self.session_factory() as session:
                    repo = SQLiteStateRepository(
                        session,
                        task_authority_mode="enforce",
                    )
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "submission_projection_conflict",
                    ):
                        repo.project_submission_admission(_record(request))

                self.assertEqual(
                    asyncio.run(
                        self.storage.get_conversation(request.conversation_id)
                    ),
                    existing,
                )

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

    def test_private_submission_input_is_hidden_preserved_and_replayable(self) -> None:
        request = _mutate_request(
            _request(),
            "message_projection",
            lambda value: value["metadata"].update(
                {
                    "__maf_private_submission_input_v1": {
                        "explicit_upload_ids": ["upload-1"]
                    }
                }
            ),
        )

        created = asyncio.run(self.storage.admit_submission(request))
        replay = asyncio.run(self.storage.admit_submission(request))
        loaded = asyncio.run(self.storage.get_message(request.message_id))

        self.assertEqual(created.disposition, SubmissionAdmissionDisposition.CREATED)
        self.assertEqual(
            replay.disposition,
            SubmissionAdmissionDisposition.IDEMPOTENT_REPLAY,
        )
        self.assertEqual(loaded.metadata, {"visible": "yes"})
        updated = asyncio.run(
            self.storage.save_message(replace(loaded, content="streamed"))
        )
        self.assertEqual(updated.metadata, {"visible": "yes"})
        with self.session_factory() as session:
            row = session.get(MessageRow, request.message_id)
            self.assertIn(
                "__maf_private_submission_input_v1",
                row.message_metadata,
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
        original_projection = storage._run_submission_projection

        async def assert_projection_precedes_ack(record) -> None:
            self.assertEqual(sidecar.ack_count, 0)
            await original_projection(record)

        storage._run_submission_projection = assert_projection_precedes_ack  # type: ignore[method-assign]

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

    def test_enforce_sql_projection_failure_does_not_ack_sidecar(self) -> None:
        sidecar = _FakeSubmissionSidecar()
        storage = SQLiteStorage(
            self.session_factory,
            runtime_sidecar_client=sidecar,
            mcp_task_authority_mode="enforce",
        )
        request = _request()
        admitted = asyncio.run(storage.admit_submission(request))
        original_projection = storage._run_submission_projection
        projection_attempts = 0

        async def fail_first_projection(record) -> None:
            nonlocal projection_attempts
            projection_attempts += 1
            if projection_attempts == 1:
                raise RuntimeError("sql-projection-fault")
            await original_projection(record)

        storage._run_submission_projection = fail_first_projection  # type: ignore[method-assign]
        acknowledgement = SubmissionProjectionAcknowledgementRequest(
            handle=admitted.handle,
            projection_sha256=request.projection_sha256,
            acknowledged_at=request.message_created_at,
        )

        with self.assertRaisesRegex(RuntimeError, "sql-projection-fault"):
            asyncio.run(storage.acknowledge_submission_projection(acknowledgement))
        with self.session_factory() as session:
            self.assertIsNone(session.get(ConversationRow, request.conversation_id))
            self.assertIsNone(session.get(MessageRow, request.message_id))
        self.assertEqual(sidecar.ack_count, 0)

        phase = asyncio.run(storage.acknowledge_submission_projection(acknowledgement))

        self.assertEqual(phase.projection_state, SubmissionProjectionState.PROJECTED)
        self.assertEqual(sidecar.ack_count, 1)
        with self.session_factory() as session:
            self.assertIsNotNone(session.get(ConversationRow, request.conversation_id))
            self.assertIsNotNone(session.get(MessageRow, request.message_id))

    def test_enforce_ack_failure_leaves_exact_projection_without_sql_task(self) -> None:
        sidecar = _FakeSubmissionSidecar()
        storage = SQLiteStorage(
            self.session_factory,
            runtime_sidecar_client=sidecar,
            mcp_task_authority_mode="enforce",
        )
        request = _request()
        admitted = asyncio.run(storage.admit_submission(request))
        sidecar.ack_error = RuntimeError("runtime_store_idempotency_conflict")

        with self.assertRaisesRegex(
            RuntimeError, "runtime_store_idempotency_conflict"
        ):
            asyncio.run(
                storage.acknowledge_submission_projection(
                    SubmissionProjectionAcknowledgementRequest(
                        handle=admitted.handle,
                        projection_sha256=request.projection_sha256,
                        acknowledged_at=request.message_created_at,
                    )
                )
            )

        with self.session_factory() as session:
            self.assertIsNotNone(session.get(ConversationRow, request.conversation_id))
            self.assertIsNotNone(session.get(MessageRow, request.message_id))
            self.assertIsNone(session.get(TaskRow, request.task.task_id))

    def test_enforce_handed_off_replay_hides_retained_sidecar_claim(self) -> None:
        sidecar = _FakeSubmissionSidecar()
        storage = SQLiteStorage(
            self.session_factory,
            runtime_sidecar_client=sidecar,
            mcp_task_authority_mode="enforce",
        )
        request = _request()
        asyncio.run(storage.admit_submission(request))
        assert sidecar.admission is not None
        sidecar.admission = {
            **sidecar.admission,
            "projection_state": "projected",
            "preparation_state": "prepared",
            "prepared_execution_json": _legacy_prepared(request),
            "prepared_execution_sha256": hashlib.sha256(
                b"maf.submission.prepared_execution.v1\0"
                + _legacy_prepared(request)
            ).hexdigest(),
            "handoff_state": "handed_off",
            "handoff_kind": "agent_run",
            "handoff_identity": f"agent-run:{request.task.task_id}",
        }

        replay = asyncio.run(storage.admit_submission(request))

        self.assertEqual(
            replay.disposition,
            SubmissionAdmissionDisposition.IDEMPOTENT_REPLAY,
        )
        self.assertEqual(replay.phase.handoff_state, SubmissionHandoffState.HANDED_OFF)
        self.assertIsNone(replay.handle)

    def test_enforce_claim_maps_cursor_scoped_pending_observability(self) -> None:
        sidecar = _FakeSubmissionSidecar()
        storage = SQLiteStorage(
            self.session_factory,
            runtime_sidecar_client=sidecar,
            mcp_task_authority_mode="enforce",
        )
        now = datetime(2026, 8, 26, tzinfo=timezone.utc)
        expires_at = now + timedelta(seconds=60)
        sidecar.claim_response = {
            "operation": "submission_pending_claim",
            "found": False,
            "admission": None,
            "claim": None,
            "authority_state": "finalized",
            "finalization_receipt_sha256": "f" * 64,
            "pending_count": 2,
            "earliest_claim_expires_at_ms": int(expires_at.timestamp() * 1000),
            "error": None,
        }

        result = asyncio.run(
            storage.claim_pending_submission(
                SubmissionClaimRequest(
                    claim_owner="worker",
                    now=now,
                    claim_expires_at=now + timedelta(seconds=30),
                )
            )
        )

        self.assertFalse(result.found)
        self.assertEqual(result.pending_count, 2)
        self.assertEqual(result.earliest_claim_expires_at, expires_at)

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


def _legacy_prepared(request: SubmissionAdmissionRequest) -> bytes:
    continuation = json.loads(request.continuation)
    return json.dumps(
        {
            "schema": "maf.submission.prepared_execution.v1",
            "task_id": request.task.task_id,
            "conversation_id": request.conversation_id,
            "message_id": request.message_id,
            "prepared_kind": "agent_run",
            "owner_scope": request.username,
            "execution_text_source": "root_message",
            "execution_text_sha256": continuation["message_content_sha256"],
            "requested_capability_id": None,
            "initial_required_tool_name": None,
            "model_options": continuation["model_options"],
            "bundle_revisions": continuation["bundle_revisions"],
            "execution_metadata": continuation["execution_metadata"],
            "preparation_receipt": {
                "task_id": request.task.task_id,
                "receipt_sha256": "a" * 64,
                "route_decision_sha256": "b" * 64,
                "memory_context_sha256": "c" * 64,
                "selector_decision_sha256": "d" * 64,
            },
            "upload_refs": continuation["upload_refs"],
            "sheet_selections": continuation["sheet_selections"],
            "mcp_binding": continuation["mcp_binding"],
            "mcp_assignment": continuation["mcp_assignment"],
            "available_mcp_servers": continuation["available_mcp_servers"],
            "pending_context": continuation["pending_context"],
            "planned_handoff_kind": "agent_run",
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


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
        self.ack_error: Exception | None = None
        self.claim_response: dict[str, object] | None = None

    def admit_submission(self, **request: object) -> dict[str, object]:
        if self.admission is not None:
            disposition = (
                "idempotent_replay"
                if self.admission["request_fingerprint"]
                == request["request_fingerprint"]
                else "message_id_conflict"
            )
            return {
                "operation": "submission_admit",
                "disposition": disposition,
                "admission": self.admission,
                "claim": {
                    "owner": request["workflow_owner"],
                    "token": "claim-secret-replay",
                    "expires_at_ms": int(request["now_ms"])
                    + int(request["claim_ttl_ms"]),
                },
                "error": None,
            }
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
        if self.ack_error is not None:
            raise self.ack_error
        assert self.admission is not None
        self.admission = {**self.admission, "projection_state": "projected"}
        return {
            "operation": "submission_projection_acknowledge",
            "admission": self.admission,
            "duplicate": False,
            "error": None,
        }

    def claim_pending_submission(self, **_request: object) -> dict[str, object]:
        assert self.claim_response is not None
        return self.claim_response
