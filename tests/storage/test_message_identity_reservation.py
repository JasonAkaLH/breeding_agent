from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from sqlalchemy.exc import IntegrityError

from src.core.enums import ConversationStatus, MessageRole
from src.core.errors import MessageIdentityConflictError
from src.core.models import (
    Conversation,
    ConversationFileResource,
    FileUploadMessageProjection,
    Message,
    MessageIdentityDisposition,
    MessageIdentityKind,
    MessageIdentityReservationRequest,
)
from src.storage.conversation_files import (
    build_file_upload_message_projection,
    file_upload_message_id,
)
from src.storage.sqlite.repositories import SQLiteStorage
from src.storage.postgres.repositories import PostgreSQLStorage
from src.storage.sqlalchemy_models import ConversationRow
from tests.storage.support import SQLiteStorageTestCase


class MessageIdentityReservationTest(SQLiteStorageTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.sidecar = _MessageIdentitySidecar()
        self.storage = SQLiteStorage(
            self.session_factory,
            runtime_sidecar_client=self.sidecar,
            mcp_task_authority_mode="enforce",
            message_identity_authority_enabled=True,
        )
        asyncio.run(
            self.storage.save_conversation(
                Conversation("conversation-1", "alice")
            )
        )

    def _mutate_conversation(
        self,
        *,
        username: str | None = None,
        status: ConversationStatus | None = None,
    ) -> None:
        with self.session_factory() as session:
            row = session.get(ConversationRow, "conversation-1")
            self.assertIsNotNone(row)
            if username is not None:
                row.username = username
            if status is not None:
                row.status = str(status)
            session.commit()

    def test_off_mode_keeps_legacy_first_insert_behavior(self) -> None:
        storage = SQLiteStorage(
            self.session_factory,
            mcp_task_authority_mode="off",
            message_identity_authority_enabled=True,
        )
        message = _message(role=MessageRole.USER)

        saved = asyncio.run(storage.save_message(message))

        self.assertEqual(saved, message)

    def test_off_mode_reservation_compares_created_at_at_protocol_milliseconds(self) -> None:
        storage = SQLiteStorage(
            self.session_factory,
            mcp_task_authority_mode="off",
        )
        message = replace(
            _message(role=MessageRole.USER),
            created_at=datetime(2026, 8, 27, 1, 2, 3, 123456),
        )
        asyncio.run(storage.save_message(message))

        result = asyncio.run(
            storage.reserve_message_identity(
                replace(
                    _interrupt_reservation(message),
                    message_created_at=message.created_at.replace(
                        microsecond=123999
                    ),
                )
            )
        )

        self.assertEqual(
            result.disposition,
            MessageIdentityDisposition.EXACT_REPLAY,
        )

    def test_shadow_mode_keeps_legacy_first_insert_behavior(self) -> None:
        storage = SQLiteStorage(
            self.session_factory,
            runtime_sidecar_client=self.sidecar,
            runtime_sidecar_shadow_sink=object(),
            mcp_task_authority_mode="shadow",
            message_identity_authority_enabled=True,
        )
        message = replace(_message(role=MessageRole.USER), message_id="shadow-message")

        saved = asyncio.run(storage.save_message(message))

        self.assertEqual(saved, message)
        self.assertEqual(self.sidecar.requests, [])

    def test_existing_mutable_update_skips_reservation_and_keeps_identity(self) -> None:
        message = _message()
        asyncio.run(
            SQLiteStorage(
                self.session_factory,
                mcp_task_authority_mode="off",
            ).save_message(message)
        )

        saved = asyncio.run(
            self.storage.save_message(
                replace(
                    message,
                    content="streamed",
                    stream_status="complete",
                    updated_at=message.created_at + timedelta(seconds=1),
                )
            )
        )

        self.assertEqual(saved.content, "streamed")
        self.assertEqual(saved.stream_status, "complete")
        self.assertEqual(self.sidecar.requests, [])

    def test_existing_immutable_rebinding_conflicts_without_reservation(self) -> None:
        message = _message()
        asyncio.run(
            SQLiteStorage(
                self.session_factory,
                mcp_task_authority_mode="off",
            ).save_message(message)
        )

        with self.assertRaises(MessageIdentityConflictError):
            asyncio.run(
                self.storage.save_message(
                    replace(message, task_id="task-other")
                )
            )

        self.assertEqual(self.sidecar.requests, [])

    def test_enforce_default_gate_keeps_pre_a5_sql_behavior(self) -> None:
        message = _message(role=MessageRole.USER)
        storage = SQLiteStorage(
            self.session_factory,
            runtime_sidecar_client=self.sidecar,
            mcp_task_authority_mode="enforce",
        )

        reserved = asyncio.run(
            storage.reserve_message_identity(_interrupt_reservation(message))
        )
        saved = asyncio.run(storage.save_message(message))
        file_projection = FileUploadMessageProjection(
            upload_id="default-gate-upload",
            conversation_id="conversation-1",
            content="ignored",
            metadata={
                "upload_id": "default-gate-upload",
                "file_status": "active",
            },
            created_at=message.created_at,
        )
        file_message = asyncio.run(
            storage.upsert_file_upload_message(
                file_projection,
                now=message.created_at,
            )
        )

        self.assertEqual(reserved.disposition, MessageIdentityDisposition.CREATED)
        self.assertEqual(saved, message)
        self.assertEqual(file_message.conversation_id, message.conversation_id)
        self.assertEqual(self.sidecar.requests, [])

    def test_enabled_authority_requires_user_interrupt_reservation(self) -> None:
        message = _message(role=MessageRole.USER)

        with self.assertRaisesRegex(
            RuntimeError, "message_identity_reservation_required"
        ):
            asyncio.run(self.storage.save_message(message))

        self.assertIsNone(asyncio.run(self.storage.get_message(message.message_id)))
        self.assertEqual(self.sidecar.requests, [])

    def test_constructor_rejects_non_boolean_authority_gate(self) -> None:
        with self.assertRaisesRegex(
            ValueError, "message_identity_authority_enabled must be a bool"
        ):
            SQLiteStorage(
                self.session_factory,
                message_identity_authority_enabled=1,  # type: ignore[arg-type]
            )

    def test_interrupt_reservation_uses_sidecar_canonical_created_at(self) -> None:
        message = _message(role=MessageRole.USER)
        first_created_at = message.created_at - timedelta(seconds=10)
        self.sidecar.disposition = MessageIdentityDisposition.EXACT_REPLAY
        self.sidecar.canonical_created_at = first_created_at
        reservation = _interrupt_reservation(message)

        saved = asyncio.run(
            self.storage.save_message(
                message,
                identity_reservation=reservation,
            )
        )

        self.assertEqual(saved.created_at, first_created_at)
        self.assertEqual(len(self.sidecar.requests), 1)
        self.assertEqual(
            self.sidecar.requests[0]["identity_kind"],
            str(MessageIdentityKind.INTERRUPT),
        )
        self.assertEqual(self.sidecar.requests[0]["username"], "alice")

    def test_interrupt_cross_conversation_tombstone_conflict_never_inserts_sql(self) -> None:
        message = _message(role=MessageRole.USER)
        self.sidecar.disposition = MessageIdentityDisposition.CONFLICT
        self.sidecar.identity_overrides = {"conversation_id": "tombstone-conversation"}

        with self.assertRaises(MessageIdentityConflictError):
            asyncio.run(
                self.storage.save_message(
                    message,
                    identity_reservation=_interrupt_reservation(message),
                )
            )

        self.assertIsNone(asyncio.run(self.storage.get_message(message.message_id)))

    def test_reservation_response_identity_mismatch_never_inserts_sql_message(self) -> None:
        message = _message(role=MessageRole.USER)
        self.sidecar.identity_overrides = {"conversation_id": "other"}

        with self.assertRaisesRegex(RuntimeError, "runtime_store_response_invalid"):
            asyncio.run(
                self.storage.save_message(
                    message,
                    identity_reservation=_interrupt_reservation(message),
                )
            )

        self.assertIsNone(asyncio.run(self.storage.get_message(message.message_id)))

    def test_non_interrupt_exact_replay_rejects_created_at_drift(self) -> None:
        message = _message()
        self.sidecar.disposition = MessageIdentityDisposition.EXACT_REPLAY
        self.sidecar.canonical_created_at = message.created_at - timedelta(seconds=1)

        with self.assertRaisesRegex(RuntimeError, "runtime_store_response_invalid"):
            asyncio.run(self.storage.save_message(message))

        self.assertIsNone(asyncio.run(self.storage.get_message(message.message_id)))

    def test_sidecar_failure_never_inserts_sql_message(self) -> None:
        message = _message()
        self.sidecar.error = RuntimeError("runtime_store_unavailable")

        with self.assertRaisesRegex(RuntimeError, "runtime_store_unavailable"):
            asyncio.run(self.storage.save_message(message))

        self.assertIsNone(asyncio.run(self.storage.get_message(message.message_id)))

    def test_server_internal_first_insert_synthesizes_reservation(self) -> None:
        message = _message()
        message = replace(message, created_at=message.created_at.replace(tzinfo=None))

        saved = asyncio.run(self.storage.save_message(message))

        self.assertEqual(
            saved.created_at.replace(tzinfo=None),
            message.created_at,
        )
        self.assertEqual(saved.content, message.content)
        self.assertEqual(len(self.sidecar.requests), 1)
        identity = self.sidecar.requests[0]
        self.assertEqual(identity["identity_kind"], str(MessageIdentityKind.SERVER_INTERNAL))
        self.assertEqual(identity["request_fingerprint"], None)
        self.assertEqual(identity["task_id"], message.task_id)

    def test_canonical_millisecond_identity_allows_streaming_update(self) -> None:
        created_at = datetime(
            2026,
            8,
            26,
            8,
            0,
            0,
            123456,
            tzinfo=timezone.utc,
        )
        message = replace(_message(), created_at=created_at)

        saved = asyncio.run(self.storage.save_message(message))
        updated = asyncio.run(
            self.storage.save_message(
                replace(
                    message,
                    content="streamed",
                    stream_status="complete",
                )
            )
        )

        self.assertEqual(saved.created_at.microsecond, 123000)
        self.assertEqual(updated.content, "streamed")
        self.assertEqual(updated.stream_status, "complete")
        self.assertEqual(len(self.sidecar.requests), 1)

    def test_missing_or_inactive_conversation_fails_before_reservation(self) -> None:
        message = replace(_message(), conversation_id="missing")

        with self.assertRaisesRegex(
            PermissionError, "Conversation is not available: missing"
        ):
            asyncio.run(self.storage.save_message(message))

        self.assertEqual(self.sidecar.requests, [])

    def test_reserved_message_rechecks_active_conversation_in_sql_transaction(self) -> None:
        message = _message()
        self.sidecar.after_reserve = lambda: self._mutate_conversation(
            status=ConversationStatus.DELETING
        )

        with self.assertRaisesRegex(
            PermissionError,
            "Conversation is not available: conversation-1",
        ):
            asyncio.run(self.storage.save_message(message))

        self.assertIsNone(asyncio.run(self.storage.get_message(message.message_id)))

    def test_file_upload_first_insert_reserves_and_update_skips_rpc(self) -> None:
        created_at = datetime(2026, 8, 26, 8, 0, tzinfo=timezone.utc)
        projection = FileUploadMessageProjection(
            upload_id="upload-1",
            conversation_id="conversation-1",
            content="ignored",
            metadata={"upload_id": "upload-1", "file_status": "active"},
            created_at=created_at,
        )

        saved = asyncio.run(
            self.storage.upsert_file_upload_message(
                projection,
                now=created_at + timedelta(seconds=1),
            )
        )
        saved_again = asyncio.run(
            self.storage.upsert_file_upload_message(
                replace(
                    projection,
                    metadata={
                        "upload_id": "upload-1",
                        "file_status": "active",
                        "description_status": "ready",
                    },
                ),
                now=created_at + timedelta(seconds=2),
            )
        )

        self.assertEqual(saved.created_at, created_at)
        self.assertEqual(saved_again.created_at, created_at)
        self.assertEqual(len(self.sidecar.requests), 1)
        identity = self.sidecar.requests[0]
        self.assertEqual(identity["identity_kind"], str(MessageIdentityKind.FILE_VISIBLE))
        self.assertEqual(identity["username"], "alice")
        self.assertEqual(identity["task_id"], None)

    def test_file_upload_rechecks_owner_in_sql_transaction(self) -> None:
        now = datetime(2026, 8, 26, 8, 0, tzinfo=timezone.utc)
        projection = FileUploadMessageProjection(
            upload_id="upload-owner-race",
            conversation_id="conversation-1",
            content="ignored",
            metadata={"upload_id": "upload-owner-race", "file_status": "active"},
            created_at=now,
        )
        self.sidecar.after_reserve = lambda: self._mutate_conversation(
            username="changed-owner"
        )

        with self.assertRaises(PermissionError) as raised:
            asyncio.run(
                self.storage.upsert_file_upload_message(projection, now=now)
            )

        self.assertEqual(
            str(raised.exception),
            "Conversation is not available: conversation-1",
        )
        self.assertNotIn("changed-owner", str(raised.exception))
        self.assertIsNone(
            asyncio.run(
                self.storage.get_message(file_upload_message_id(projection.upload_id))
            )
        )

    def test_existing_file_upload_immutable_tuple_conflicts_without_mutation(self) -> None:
        now = datetime(2026, 8, 26, 8, 0, tzinfo=timezone.utc)
        message_id = file_upload_message_id("upload-immutable")
        original = Message(
            message_id=message_id,
            conversation_id="conversation-1",
            role=MessageRole.SYSTEM,
            content="original",
            task_id="unexpected-task",
            stream_status="complete",
            created_at=now,
            message_type="file_upload",
            metadata={"upload_id": "upload-immutable", "file_status": "active"},
        )
        asyncio.run(
            SQLiteStorage(
                self.session_factory,
                mcp_task_authority_mode="off",
            ).save_message(original)
        )
        projection = FileUploadMessageProjection(
            upload_id="upload-immutable",
            conversation_id="conversation-1",
            content="replacement",
            metadata={"upload_id": "upload-immutable", "file_status": "active"},
            created_at=now,
        )

        with self.assertRaises(MessageIdentityConflictError):
            asyncio.run(
                self.storage.upsert_file_upload_message(projection, now=now)
            )

        loaded = asyncio.run(self.storage.get_message(message_id))
        self.assertEqual(loaded.content, original.content)
        self.assertEqual(loaded.task_id, original.task_id)
        self.assertEqual(self.sidecar.requests, [])

    def test_existing_file_upload_explicit_created_at_drift_conflicts(self) -> None:
        now = datetime(2026, 8, 26, 8, 0, tzinfo=timezone.utc)
        projection = FileUploadMessageProjection(
            upload_id="upload-created-at",
            conversation_id="conversation-1",
            content="original",
            metadata={"upload_id": "upload-created-at", "file_status": "active"},
            created_at=now,
        )
        asyncio.run(
            SQLiteStorage(
                self.session_factory,
                mcp_task_authority_mode="off",
            ).upsert_file_upload_message(projection, now=now)
        )

        with self.assertRaises(MessageIdentityConflictError):
            asyncio.run(
                self.storage.upsert_file_upload_message(
                    replace(projection, created_at=now + timedelta(seconds=1)),
                    now=now + timedelta(seconds=1),
                )
            )

        loaded = asyncio.run(
            self.storage.get_message(file_upload_message_id(projection.upload_id))
        )
        self.assertEqual(loaded.created_at, now)
        self.assertEqual(self.sidecar.requests, [])

    def test_composite_file_upload_uses_resource_owner_before_atomic_write(self) -> None:
        resource = ConversationFileResource(
            file_id="upload-composite",
            conversation_id="conversation-1",
            username="alice",
            original_filename="data.csv",
            content_type="text/csv",
            file_type="csv",
            size_bytes=4,
            sha256="a" * 64,
            storage_key="conversation-1/upload-composite/original",
            status="active",
            created_at=datetime(2026, 8, 26, 8, 0, tzinfo=timezone.utc),
        )

        saved = asyncio.run(
            self.storage.save_conversation_file_resource_with_upload_message(
                resource,
                build_file_upload_message_projection(resource),
                now=resource.created_at,
            )
        )

        self.assertEqual(saved, resource)
        self.assertEqual(len(self.sidecar.requests), 1)
        self.assertEqual(self.sidecar.requests[0]["username"], resource.username)
        self.assertIsNotNone(
            asyncio.run(
                self.storage.get_message(file_upload_message_id(resource.file_id))
            )
        )

    def test_composite_reservation_conflict_leaves_resource_and_message_absent(self) -> None:
        self.sidecar.disposition = MessageIdentityDisposition.CONFLICT
        resource = ConversationFileResource(
            file_id="upload-conflict",
            conversation_id="conversation-1",
            username="alice",
            original_filename="data.csv",
            content_type="text/csv",
            file_type="csv",
            size_bytes=4,
            sha256="b" * 64,
            storage_key="conversation-1/upload-conflict/original",
            status="active",
            created_at=datetime(2026, 8, 26, 8, 0, tzinfo=timezone.utc),
        )

        with self.assertRaises(MessageIdentityConflictError):
            asyncio.run(
                self.storage.save_conversation_file_resource_with_upload_message(
                    resource,
                    build_file_upload_message_projection(resource),
                    now=resource.created_at,
                )
            )

        self.assertIsNone(
            asyncio.run(
                self.storage.get_conversation_file_resource_by_id(resource.file_id)
            )
        )
        self.assertIsNone(
            asyncio.run(
                self.storage.get_message(file_upload_message_id(resource.file_id))
            )
        )

    def test_composite_rechecks_active_conversation_before_resource_write(self) -> None:
        resource = ConversationFileResource(
            file_id="upload-composite-race",
            conversation_id="conversation-1",
            username="alice",
            original_filename="data.csv",
            content_type="text/csv",
            file_type="csv",
            size_bytes=4,
            sha256="c" * 64,
            storage_key="conversation-1/upload-composite-race/original",
            status="active",
            created_at=datetime(2026, 8, 26, 8, 0, tzinfo=timezone.utc),
        )
        self.sidecar.after_reserve = lambda: self._mutate_conversation(
            status=ConversationStatus.DELETING
        )

        with self.assertRaisesRegex(
            PermissionError,
            "Conversation is not available: conversation-1",
        ):
            asyncio.run(
                self.storage.save_conversation_file_resource_with_upload_message(
                    resource,
                    build_file_upload_message_projection(resource),
                    now=resource.created_at,
                )
            )

        self.assertIsNone(
            asyncio.run(
                self.storage.get_conversation_file_resource_by_id(resource.file_id)
            )
        )
        self.assertIsNone(
            asyncio.run(
                self.storage.get_message(file_upload_message_id(resource.file_id))
            )
        )


def _message(*, role: MessageRole = MessageRole.ASSISTANT) -> Message:
    return Message(
        message_id="message-1",
        conversation_id="conversation-1",
        role=role,
        content="hello",
        task_id="task-1",
        stream_status="streaming",
        created_at=datetime(2026, 8, 26, 8, 0, tzinfo=timezone.utc),
    )


def _interrupt_reservation(message: Message) -> MessageIdentityReservationRequest:
    return MessageIdentityReservationRequest(
        username="alice",
        conversation_id=message.conversation_id,
        message_id=message.message_id,
        identity_kind=MessageIdentityKind.INTERRUPT,
        role=message.role,
        message_type=message.message_type,
        message_created_at=message.created_at,
        task_id=message.task_id,
        request_fingerprint="f" * 64,
        reserved_at=message.created_at,
    )


class _MessageIdentitySidecar:
    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []
        self.disposition = MessageIdentityDisposition.CREATED
        self.canonical_created_at: datetime | None = None
        self.identity_overrides: dict[str, object] = {}
        self.error: Exception | None = None
        self.after_reserve: Callable[[], None] | None = None

    def reserve_message_identity(self, *, identity: dict[str, object]) -> dict[str, object]:
        self.requests.append(dict(identity))
        if self.error is not None:
            raise self.error
        if self.after_reserve is not None:
            self.after_reserve()
        canonical_created_at = self.canonical_created_at
        canonical_identity = dict(identity)
        if canonical_created_at is not None:
            canonical_identity["message_created_at_ms"] = int(
                canonical_created_at.timestamp() * 1000
            )
        canonical_identity.update(self.identity_overrides)
        return {
            "operation": "message_identity_reserve",
            "disposition": str(self.disposition),
            "identity": canonical_identity,
            "error": None,
        }


class PostgreSQLMessageWriteRetryTest(SQLiteStorageTestCase):
    def test_enforce_default_gate_does_not_enable_unique_retry(self) -> None:
        storage = PostgreSQLStorage(
            self.session_factory,
            runtime_sidecar_client=object(),
            mcp_task_authority_mode="enforce",
        )
        unique_error = IntegrityError(None, None, _PostgresError("23505"))

        with patch.object(
            storage,
            "_run",
            new=AsyncMock(side_effect=unique_error),
        ) as run, self.assertRaises(IntegrityError):
            asyncio.run(storage.save_message(_message()))

        self.assertEqual(run.await_count, 1)

    def test_reserved_insert_unique_violation_retries_once(self) -> None:
        storage = PostgreSQLStorage(
            self.session_factory,
            mcp_task_authority_mode="off",
        )
        unique_error = IntegrityError(None, None, _PostgresError("23505"))

        with patch.object(
            storage,
            "_run",
            new=AsyncMock(side_effect=(unique_error, "saved")),
        ) as run:
            saved = asyncio.run(
                storage._run_message_write(
                    lambda state: state,
                    retry_unique=True,
                )
            )

        self.assertEqual(saved, "saved")
        self.assertEqual(run.await_count, 2)

    def test_off_and_shadow_unique_violation_is_not_retried(self) -> None:
        for mode in ("off", "shadow"):
            storage = PostgreSQLStorage(
                self.session_factory,
                runtime_sidecar_client=(object() if mode == "shadow" else None),
                runtime_sidecar_shadow_sink=(object() if mode == "shadow" else None),
                mcp_task_authority_mode=mode,
            )
            unique_error = IntegrityError(None, None, _PostgresError("23505"))

            with self.subTest(mode=mode), patch.object(
                storage,
                "_run",
                new=AsyncMock(side_effect=unique_error),
            ) as run, self.assertRaises(IntegrityError):
                asyncio.run(storage._run_message_write(lambda state: state))

            self.assertEqual(run.await_count, 1)

    def test_non_unique_integrity_error_is_not_retried(self) -> None:
        storage = PostgreSQLStorage(
            self.session_factory,
            mcp_task_authority_mode="off",
        )
        constraint_error = IntegrityError(None, None, _PostgresError("23503"))

        with patch.object(
            storage,
            "_run",
            new=AsyncMock(side_effect=constraint_error),
        ) as run, self.assertRaises(IntegrityError):
            asyncio.run(
                storage._run_message_write(
                    lambda state: state,
                    retry_unique=True,
                )
            )

        self.assertEqual(run.await_count, 1)


class _PostgresError(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__(sqlstate)
        self.sqlstate = sqlstate
