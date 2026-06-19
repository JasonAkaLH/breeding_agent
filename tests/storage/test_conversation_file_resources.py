from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import inspect, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from src.core.enums import MessageRole
from src.core.models import ConversationFileResource, FileUploadMessageProjection, Message
from src.storage.conversation_files import (
    FILE_UPLOAD_MESSAGE_FORBIDDEN_METADATA_KEYS,
    FILE_UPLOAD_MESSAGE_MARKED_DELETED_EVENT,
    FILE_UPLOAD_MESSAGE_TYPE,
    FILE_UPLOAD_MESSAGE_UPSERTED_EVENT,
    build_file_upload_message_projection,
    file_upload_message_audit_payload,
    file_upload_message_id,
)
from src.storage.conversation_files import ConversationFileIndexWriter, LocalConversationFileStore
from src.storage.sqlite import SQLiteStorage
from src.storage.sqlite.models import ConversationFileResourceRow, EventRecordRow, MessageRow
from tests.storage.support import SQLiteStorageTestCase


class ConversationFileResourceRepositoryTest(SQLiteStorageTestCase):
    def _file_upload_audit_payloads(self, event_type: str) -> list[dict]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(EventRecordRow).where(EventRecordRow.event_type == event_type).order_by(EventRecordRow.created_at)
            ).all()
        return [dict(row.payload or {}) for row in rows]

    def _resource(self, file_id: str = "upl-1", *, conversation_id: str = "conv-1") -> ConversationFileResource:
        return ConversationFileResource(
            file_id=file_id,
            conversation_id=conversation_id,
            username="alice",
            original_filename="materials.csv",
            content_type="text/csv",
            file_type="csv",
            size_bytes=12,
            sha256=f"sha-{file_id}",
            storage_key=f"{conversation_id}/{file_id}/original",
            preview={"row_count": 1, "columns": ["ped_id"]},
            description_status="ready",
            description_summary="CSV material file",
            status="active",
            created_at=datetime(2026, 6, 16, 7, 0, 0),
            updated_at=datetime(2026, 6, 16, 7, 1, 0),
        )

    def test_bootstrap_creates_conversation_file_resource_table_and_indexes(self) -> None:
        inspector = inspect(self.engine)
        self.assertIn("conversation_file_resource", set(inspector.get_table_names()))
        index_names = {index["name"] for index in inspector.get_indexes("conversation_file_resource")}
        self.assertIn("idx_conversation_file_conversation_status_created", index_names)
        self.assertIn("idx_conversation_file_username_conversation", index_names)
        self.assertIn("conversation_file_index_repair_marker", set(inspector.get_table_names()))
        marker_index_names = {index["name"] for index in inspector.get_indexes("conversation_file_index_repair_marker")}
        self.assertIn("idx_conversation_file_index_repair_status_retry", marker_index_names)

    def test_repair_marker_round_trip_merge_due_and_lifecycle(self) -> None:
        storage = SQLiteStorage(self.session_factory)
        first_at = datetime(2026, 6, 16, 7, 0, 0)
        second_at = datetime(2026, 6, 16, 7, 0, 10)

        first = asyncio.run(
            storage.record_conversation_file_index_repair_required(
                "conv-repair",
                reason_code="upload_index_write_failed",
                affected_upload_ids=("upl-a",),
                now=first_at,
            )
        )
        second = asyncio.run(
            storage.record_conversation_file_index_repair_required(
                "conv-repair",
                reason_code="delete_index_write_failed",
                affected_upload_ids=("upl-a", "upl-b"),
                now=second_at,
            )
        )
        loaded = asyncio.run(storage.get_conversation_file_index_repair_marker("conv-repair"))
        due_before_retry = asyncio.run(
            storage.list_due_conversation_file_index_repairs(now=first_at + timedelta(seconds=1))
        )
        due_after_retry = asyncio.run(
            storage.list_due_conversation_file_index_repairs(now=second.next_retry_at, limit=1)
        )
        repairing = asyncio.run(
            storage.mark_conversation_file_index_repairing("conv-repair", now=second_at + timedelta(seconds=1))
        )
        failed = asyncio.run(
            storage.mark_conversation_file_index_repair_failed(
                "conv-repair",
                reason_code="index_repair_failed",
                now=second_at + timedelta(seconds=2),
            )
        )
        resolved = asyncio.run(
            storage.mark_conversation_file_index_repair_resolved(
                "conv-repair",
                now=second_at + timedelta(seconds=3),
            )
        )
        asyncio.run(
            storage.record_conversation_file_index_repair_required(
                "conv-nonretryable",
                reason_code="conversation_missing",
                affected_upload_ids=(),
                now=first_at,
            )
        )
        nonretryable_failed = asyncio.run(
            storage.mark_conversation_file_index_repair_failed(
                "conv-nonretryable",
                reason_code="conversation_missing",
                now=first_at + timedelta(seconds=1),
                retryable=False,
            )
        )
        due_after_nonretryable = asyncio.run(
            storage.list_due_conversation_file_index_repairs(now=first_at + timedelta(days=1))
        )

        self.assertEqual(first.status, "pending")
        self.assertEqual(first.attempt_count, 1)
        self.assertEqual(first.next_retry_at, first_at + timedelta(seconds=5))
        self.assertEqual(second.created_at, first_at)
        self.assertEqual(second.attempt_count, 2)
        self.assertEqual(second.reason_code, "delete_index_write_failed")
        self.assertEqual(second.affected_upload_ids, ("upl-a", "upl-b"))
        self.assertEqual(loaded, second)
        self.assertEqual(due_before_retry, [])
        self.assertEqual([marker.conversation_id for marker in due_after_retry], ["conv-repair"])
        self.assertEqual(repairing.status, "repairing")
        self.assertEqual(repairing.attempt_count, 3)
        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.reason_code, "index_repair_failed")
        self.assertEqual(failed.next_retry_at, second_at + timedelta(seconds=122))
        self.assertEqual(resolved.status, "resolved")
        self.assertIsNone(resolved.next_retry_at)
        self.assertEqual(resolved.resolved_at, second_at + timedelta(seconds=3))
        self.assertEqual(nonretryable_failed.status, "failed")
        self.assertIsNone(nonretryable_failed.next_retry_at)
        self.assertNotIn("conv-nonretryable", {marker.conversation_id for marker in due_after_nonretryable})

    def test_composite_upload_save_and_compensation_manage_resource_and_history(self) -> None:
        storage = SQLiteStorage(self.session_factory)
        resource = self._resource("upl-composite", conversation_id="conv-composite")
        now = datetime(2026, 6, 16, 7, 0, 1)

        saved = asyncio.run(
            storage.save_conversation_file_resource_with_upload_message(
                resource,
                build_file_upload_message_projection(resource),
                now=now,
            )
        )
        message = asyncio.run(storage.get_message(file_upload_message_id("upl-composite")))
        compensation = asyncio.run(
            storage.compensate_failed_conversation_file_upload(
                "conv-composite",
                "alice",
                "upl-composite",
                reason_code="index_write_failed",
                now=now + timedelta(seconds=1),
            )
        )
        resource_after = asyncio.run(
            storage.get_conversation_file_resource("conv-composite", "alice", "upl-composite")
        )
        message_after = asyncio.run(storage.get_message(file_upload_message_id("upl-composite")))

        self.assertEqual(saved, resource)
        self.assertIsNotNone(message)
        self.assertEqual(message.message_type, FILE_UPLOAD_MESSAGE_TYPE)
        self.assertEqual(compensation["status"], "removed")
        self.assertEqual(compensation["resource_deleted"], 1)
        self.assertEqual(compensation["message_deleted"], 1)
        self.assertIsNone(resource_after)
        self.assertIsNone(message_after)

    def test_postgresql_boolean_default_uses_false_literal(self) -> None:
        ddl = str(CreateTable(ConversationFileResourceRow.__table__).compile(dialect=postgresql.dialect()))

        self.assertIn("requires_sheet_selection BOOLEAN DEFAULT false NOT NULL", ddl)
        self.assertNotIn("requires_sheet_selection BOOLEAN DEFAULT 0", ddl)

    def test_repository_round_trip_list_get_and_mark_deleted(self) -> None:
        resource = ConversationFileResource(
            file_id="upl-1",
            conversation_id="conv-1",
            username="alice",
            original_filename="materials.csv",
            content_type="text/csv",
            file_type="csv",
            size_bytes=12,
            sha256="sha-1",
            storage_key="conv-1/upl-1/original",
            preview={"row_count": 1, "columns": ["ped_id"]},
            description_status="ready",
            description_summary="CSV material file",
            status="active",
            created_at=datetime(2026, 6, 16, 7, 0, 0),
            updated_at=datetime(2026, 6, 16, 7, 1, 0),
        )
        storage = SQLiteStorage(self.session_factory)

        saved = asyncio.run(storage.save_conversation_file_resource(resource))
        loaded = asyncio.run(storage.get_conversation_file_resource("conv-1", "alice", "upl-1"))
        listed = asyncio.run(storage.list_conversation_file_resources("conv-1", "alice"))
        wrong_owner = asyncio.run(storage.get_conversation_file_resource("conv-1", "bob", "upl-1"))
        deleted = asyncio.run(
            storage.mark_conversation_file_resource_deleted(
                "conv-1",
                "alice",
                "upl-1",
                updated_at=datetime(2026, 6, 16, 7, 2, 0),
            )
        )
        active_after_delete = asyncio.run(storage.list_conversation_file_resources("conv-1", "alice"))
        all_after_delete = asyncio.run(storage.list_conversation_file_resources("conv-1", "alice", include_deleted=True))

        self.assertEqual(saved, resource)
        self.assertEqual(loaded, resource)
        self.assertEqual(listed, [resource])
        self.assertIsNone(wrong_owner)
        self.assertEqual(deleted.status, "deleted")
        self.assertEqual(active_after_delete, [])
        self.assertEqual(all_after_delete[0].status, "deleted")

    def test_file_upload_projection_contains_only_safe_allowlisted_metadata(self) -> None:
        resource = ConversationFileResource(
            file_id="upl-123abc456def",
            conversation_id="conv-safe",
            username="alice",
            original_filename="materials.csv",
            content_type="text/csv",
            file_type="csv",
            size_bytes=12,
            sha256="sha-safe",
            storage_key="/tmp/secret/materials.csv",
            preview={
                "row_count": 10,
                "column_count": 3,
                "excel_sheets": [{"sheet_name": "Sheet1"}],
                "content": "do not leak",
            },
            description_status="ready",
            description_summary="Materials table",
            status="active",
            selected_sheet="Sheet1",
            requires_sheet_selection=True,
            created_at=datetime(2026, 6, 16, 7, 0, 0),
            updated_at=datetime(2026, 6, 16, 7, 1, 0),
        )

        projection = build_file_upload_message_projection(resource)
        metadata_json = json.dumps(dict(projection.metadata), ensure_ascii=False, sort_keys=True)

        self.assertEqual(projection.upload_id, "upl-123abc456def")
        self.assertEqual(projection.conversation_id, "conv-safe")
        self.assertEqual(projection.metadata["upload_id"], "upl-123abc456def")
        self.assertEqual(projection.metadata["filename"], "materials.csv")
        self.assertEqual(projection.metadata["row_count"], 10)
        self.assertEqual(projection.metadata["column_count"], 3)
        self.assertEqual(projection.metadata["sheet_names"], ["Sheet1"])
        for forbidden in FILE_UPLOAD_MESSAGE_FORBIDDEN_METADATA_KEYS:
            self.assertNotIn(forbidden, projection.metadata)
        for leaked_value in ("/tmp/secret", "storage_key", "mount_path", "content_base64", "do not leak"):
            self.assertNotIn(leaked_value, metadata_json)

    def test_file_upload_message_upsert_insert_and_update_keep_stable_identity(self) -> None:
        storage = SQLiteStorage(self.session_factory)
        created_at = datetime(2026, 6, 16, 7, 0, 0)
        first = build_file_upload_message_projection(
            ConversationFileResource(
                file_id="upl-123abc456def",
                conversation_id="conv-file-history",
                username="alice",
                original_filename="materials.csv",
                content_type="text/csv",
                file_type="csv",
                size_bytes=12,
                sha256="sha-1",
                storage_key="conv/upl/original",
                description_status="pending",
                status="active",
                created_at=created_at,
            )
        )
        updated = build_file_upload_message_projection(
            ConversationFileResource(
                file_id="upl-123abc456def",
                conversation_id="conv-file-history",
                username="alice",
                original_filename="materials.csv",
                content_type="text/csv",
                file_type="csv",
                size_bytes=12,
                sha256="sha-1",
                storage_key="conv/upl/original",
                description_status="ready",
                description_summary="Ready summary",
                status="active",
                created_at=created_at,
                updated_at=datetime(2026, 6, 16, 7, 2, 0),
            )
        )

        saved = asyncio.run(storage.upsert_file_upload_message(first, now=datetime(2026, 6, 16, 7, 0, 1)))
        saved_again = asyncio.run(storage.upsert_file_upload_message(updated, now=datetime(2026, 6, 16, 7, 3, 0)))
        listed = asyncio.run(storage.list_messages_for_conversation("conv-file-history"))

        self.assertEqual(saved.message_id, file_upload_message_id("upl-123abc456def"))
        self.assertEqual(saved.role, MessageRole.SYSTEM)
        self.assertEqual(saved.message_type, FILE_UPLOAD_MESSAGE_TYPE)
        self.assertEqual(saved.stream_status, "complete")
        self.assertEqual(saved.created_at, created_at)
        self.assertEqual(saved.metadata["description_status"], "pending")
        self.assertEqual(saved_again.message_id, saved.message_id)
        self.assertEqual(saved_again.created_at, created_at)
        self.assertEqual(saved_again.updated_at, datetime(2026, 6, 16, 7, 3, 0))
        self.assertEqual(saved_again.metadata["description_summary"], "Ready summary")
        self.assertEqual(len(listed), 1)
        audit_payloads = self._file_upload_audit_payloads(FILE_UPLOAD_MESSAGE_UPSERTED_EVENT)
        self.assertEqual([payload["outcome"] for payload in audit_payloads], ["inserted", "updated"])
        self.assertEqual(audit_payloads[-1]["metadata_keys"], sorted(saved_again.metadata))

    def test_file_upload_message_upsert_filters_unsafe_projection_metadata_and_content(self) -> None:
        storage = SQLiteStorage(self.session_factory)
        projection = FileUploadMessageProjection(
            upload_id="upl-unsafe",
            conversation_id="conv-unsafe",
            content="malicious content with /tmp/secret and content_base64",
            metadata={
                "upload_id": "wrong-id",
                "filename": "safe.csv",
                "file_status": "active",
                "storage_key": "/tmp/secret/file.csv",
                "content": "raw file content",
                "content_base64": "abc",
            },
            created_at=datetime(2026, 6, 16, 7, 0, 0),
        )

        saved = asyncio.run(storage.upsert_file_upload_message(projection, now=datetime(2026, 6, 16, 7, 0, 1)))
        saved_json = json.dumps(dict(saved.metadata), ensure_ascii=False, sort_keys=True)

        self.assertEqual(saved.metadata["upload_id"], "upl-unsafe")
        self.assertEqual(saved.metadata["filename"], "safe.csv")
        self.assertNotIn("storage_key", saved.metadata)
        self.assertNotIn("content", saved.metadata)
        self.assertNotIn("/tmp/secret", saved_json)
        self.assertNotIn("content_base64", saved.content)
        self.assertNotIn("/tmp/secret", saved.content)

    def test_file_upload_message_upsert_conflicts_fail_closed(self) -> None:
        storage = SQLiteStorage(self.session_factory)
        now = datetime(2026, 6, 16, 7, 0, 0)
        asyncio.run(
            storage.save_message(
                Message(
                    file_upload_message_id("upl-conflict"),
                    "conv-conflict",
                    MessageRole.USER,
                    "ordinary chat collision",
                    created_at=now,
                )
            )
        )
        projection = FileUploadMessageProjection(
            upload_id="upl-conflict",
            conversation_id="conv-conflict",
            content="file",
            metadata={"upload_id": "upl-conflict", "file_status": "active"},
            created_at=now,
        )

        with self.assertRaisesRegex(ValueError, "non-file_upload"):
            asyncio.run(storage.upsert_file_upload_message(projection, now=now))
        failure_payloads = self._file_upload_audit_payloads(FILE_UPLOAD_MESSAGE_UPSERTED_EVENT)
        self.assertEqual(failure_payloads[-1]["outcome"], "failed")
        self.assertEqual(failure_payloads[-1]["reason_code"], "message_type_conflict")

        existing = Message(
            file_upload_message_id("upl-other-conv"),
            "conv-a",
            MessageRole.SYSTEM,
            "file",
            created_at=now,
            message_type=FILE_UPLOAD_MESSAGE_TYPE,
            metadata={"upload_id": "upl-other-conv", "file_status": "active"},
        )
        asyncio.run(storage.save_message(existing))
        other_conversation_projection = FileUploadMessageProjection(
            upload_id="upl-other-conv",
            conversation_id="conv-b",
            content="file",
            metadata={"upload_id": "upl-other-conv", "file_status": "active"},
            created_at=now,
        )
        with self.assertRaisesRegex(ValueError, "another conversation"):
            asyncio.run(storage.upsert_file_upload_message(other_conversation_projection, now=now))
        failure_payloads = self._file_upload_audit_payloads(FILE_UPLOAD_MESSAGE_UPSERTED_EVENT)
        self.assertEqual(failure_payloads[-1]["outcome"], "failed")
        self.assertEqual(failure_payloads[-1]["reason_code"], "conversation_mismatch")

    def test_file_upload_mark_deleted_no_backfill_and_no_resurrection(self) -> None:
        storage = SQLiteStorage(self.session_factory)
        created_at = datetime(2026, 6, 16, 7, 0, 0)
        projection = FileUploadMessageProjection(
            upload_id="upl-delete",
            conversation_id="conv-delete",
            content="active",
            metadata={"schema_version": 1, "upload_id": "upl-delete", "filename": "a.csv", "file_status": "active"},
            created_at=created_at,
        )

        missing = asyncio.run(
            storage.mark_file_upload_message_deleted(
                "conv-delete",
                "upl-missing",
                deleted_at=datetime(2026, 6, 16, 7, 5, 0),
            )
        )
        saved = asyncio.run(storage.upsert_file_upload_message(projection, now=datetime(2026, 6, 16, 7, 1, 0)))
        deleted = asyncio.run(
            storage.mark_file_upload_message_deleted(
                "conv-delete",
                "upl-delete",
                deleted_at=datetime(2026, 6, 16, 7, 6, 0),
            )
        )

        self.assertIsNone(missing)
        self.assertEqual(deleted.created_at, saved.created_at)
        self.assertEqual(deleted.updated_at, datetime(2026, 6, 16, 7, 6, 0))
        self.assertEqual(deleted.metadata["file_status"], "deleted")
        self.assertIn("已删除", deleted.content)
        with self.assertRaisesRegex(ValueError, "resurrected"):
            asyncio.run(storage.upsert_file_upload_message(projection, now=datetime(2026, 6, 16, 7, 7, 0)))
        delete_payloads = self._file_upload_audit_payloads(FILE_UPLOAD_MESSAGE_MARKED_DELETED_EVENT)
        self.assertEqual([payload["outcome"] for payload in delete_payloads], ["noop", "marked_deleted"])
        self.assertEqual(delete_payloads[0]["reason_code"], "message_missing")
        upsert_payloads = self._file_upload_audit_payloads(FILE_UPLOAD_MESSAGE_UPSERTED_EVENT)
        self.assertEqual(upsert_payloads[-1]["outcome"], "failed")
        self.assertEqual(upsert_payloads[-1]["reason_code"], "deleted_no_resurrection")

    def test_message_metadata_non_object_rows_read_as_empty_public_metadata(self) -> None:
        storage = SQLiteStorage(self.session_factory)
        with self.session_factory() as session:
            session.add(
                MessageRow(
                    message_id="msg-list-metadata",
                    conversation_id="conv-metadata",
                    role="user",
                    content="hello",
                    message_type="chat",
                    message_metadata=["not", "object"],
                    created_at=datetime(2026, 6, 16, 7, 0, 0),
                )
            )
            session.commit()

        loaded = asyncio.run(storage.get_message("msg-list-metadata"))

        self.assertEqual(loaded.metadata, {})

    def test_file_upload_audit_payload_uses_safe_summary_only(self) -> None:
        projection = FileUploadMessageProjection(
            upload_id="upl-audit",
            conversation_id="conv-audit",
            content="file",
            metadata={"upload_id": "upl-audit", "filename": "safe.csv", "file_status": "active"},
        )

        payload = file_upload_message_audit_payload(
            event_type=FILE_UPLOAD_MESSAGE_UPSERTED_EVENT,
            conversation_id="conv-audit",
            upload_id="upl-audit",
            outcome="inserted",
            projection=projection,
        )
        deleted_payload = file_upload_message_audit_payload(
            event_type=FILE_UPLOAD_MESSAGE_MARKED_DELETED_EVENT,
            conversation_id="conv-audit",
            upload_id="upl-audit",
            outcome="marked_deleted",
            projection=projection,
        )
        payload_json = json.dumps({"upsert": payload, "delete": deleted_payload}, ensure_ascii=False, sort_keys=True)

        self.assertEqual(payload["message_id"], "file_upload:upl-audit")
        self.assertEqual(deleted_payload["event_type"], FILE_UPLOAD_MESSAGE_MARKED_DELETED_EVENT)
        for forbidden in FILE_UPLOAD_MESSAGE_FORBIDDEN_METADATA_KEYS:
            self.assertNotIn(forbidden, payload_json)

    def test_cursor_pagination_uses_created_at_order_not_random_upload_id_order(self) -> None:
        storage = SQLiteStorage(self.session_factory)
        resources = [
            ConversationFileResource(
                file_id="upl-z",
                conversation_id="conv-page",
                username="alice",
                original_filename="first.csv",
                content_type="text/csv",
                file_type="csv",
                size_bytes=1,
                sha256="sha-z",
                storage_key="conv-page/upl-z/original",
                description_status="ready",
                status="active",
                created_at=datetime(2026, 6, 16, 7, 0, 0),
            ),
            ConversationFileResource(
                file_id="upl-a",
                conversation_id="conv-page",
                username="alice",
                original_filename="second.csv",
                content_type="text/csv",
                file_type="csv",
                size_bytes=1,
                sha256="sha-a",
                storage_key="conv-page/upl-a/original",
                description_status="ready",
                status="active",
                created_at=datetime(2026, 6, 16, 7, 1, 0),
            ),
        ]
        for resource in resources:
            asyncio.run(storage.save_conversation_file_resource(resource))

        first_page = asyncio.run(storage.list_conversation_file_resources("conv-page", "alice", limit=1))
        second_page = asyncio.run(
            storage.list_conversation_file_resources("conv-page", "alice", limit=1, cursor=first_page[-1].file_id)
        )

        self.assertEqual([item.file_id for item in first_page], ["upl-z"])
        self.assertEqual([item.file_id for item in second_page], ["upl-a"])

    def test_cursor_pagination_continues_when_cursor_resource_was_deleted(self) -> None:
        storage = SQLiteStorage(self.session_factory)
        for file_id, created_at in (
            ("upl-z", datetime(2026, 6, 16, 7, 0, 0)),
            ("upl-a", datetime(2026, 6, 16, 7, 1, 0)),
        ):
            asyncio.run(
                storage.save_conversation_file_resource(
                    ConversationFileResource(
                        file_id=file_id,
                        conversation_id="conv-page-delete",
                        username="alice",
                        original_filename=f"{file_id}.csv",
                        content_type="text/csv",
                        file_type="csv",
                        size_bytes=1,
                        sha256=f"sha-{file_id}",
                        storage_key=f"conv-page-delete/{file_id}/original",
                        description_status="ready",
                        status="active",
                        created_at=created_at,
                    )
                )
            )
        asyncio.run(
            storage.mark_conversation_file_resource_deleted(
                "conv-page-delete",
                "alice",
                "upl-z",
                updated_at=datetime(2026, 6, 16, 7, 2, 0),
            )
        )

        second_page = asyncio.run(
            storage.list_conversation_file_resources("conv-page-delete", "alice", limit=1, cursor="upl-z")
        )

        self.assertEqual([item.file_id for item in second_page], ["upl-a"])


class LocalConversationFileStoreTest(unittest.TestCase):
    def test_save_original_and_index_use_safe_relative_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = LocalConversationFileStore(Path(tmpdir) / "conversation_files")
            saved = store.save_original(conversation_id="conv/unsafe", upload_id="upl-1", content=b"abc")
            self.assertEqual(saved.storage_key, "conv%2Funsafe/upl-1/original")
            self.assertEqual(store.read_bytes(saved.storage_key), b"abc")

            resource = ConversationFileResource(
                file_id="upl-1",
                conversation_id="conv/unsafe",
                username="alice",
                original_filename="materials.csv",
                content_type="text/csv",
                file_type="csv",
                size_bytes=3,
                sha256=saved.sha256,
                storage_key=saved.storage_key,
                preview={"row_count": 1, "columns": ["ped_id"]},
                description_status="ready",
                description_summary="Materials table",
                status="active",
                created_at=datetime(2026, 6, 16, 7, 0, 0),
            )
            index_path = ConversationFileIndexWriter(store).write_index(
                conversation_id="conv/unsafe",
                resources=[resource],
            )
            index_text = index_path.read_text(encoding="utf-8")

            self.assertIn("upl-1 — materials.csv", index_text)
            self.assertIn("相对路径: upl-1/original", index_text)
            self.assertIn("Materials table", index_text)
            with self.assertRaises(ValueError):
                store.read_bytes("../escape/upl/original")

    def test_storage_component_encoding_does_not_collapse_distinct_conversation_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = LocalConversationFileStore(Path(tmpdir) / "conversation_files")
            slash = store.save_original(conversation_id="conv/unsafe", upload_id="upl-1", content=b"slash")
            underscore = store.save_original(conversation_id="conv_unsafe", upload_id="upl-1", content=b"underscore")

            self.assertNotEqual(slash.storage_key.split("/", 1)[0], underscore.storage_key.split("/", 1)[0])
            self.assertEqual(store.read_bytes(slash.storage_key), b"slash")
            self.assertEqual(store.read_bytes(underscore.storage_key), b"underscore")

    def test_delete_resource_dir_removes_one_upload_without_deleting_conversation_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = LocalConversationFileStore(Path(tmpdir) / "conversation_files")
            first = store.save_original(conversation_id="conv-1", upload_id="upl-1", content=b"first")
            second = store.save_original(conversation_id="conv-1", upload_id="upl-2", content=b"second")
            index_path = store.conversation_dir("conv-1") / "index.md"
            index_path.write_text("index", encoding="utf-8")

            deleted = store.delete_resource_dir(conversation_id="conv-1", upload_id="upl-1")

            self.assertTrue(deleted)
            with self.assertRaises(FileNotFoundError):
                store.read_bytes(first.storage_key)
            self.assertEqual(store.read_bytes(second.storage_key), b"second")
            self.assertEqual(index_path.read_text(encoding="utf-8"), "index")


if __name__ == "__main__":
    unittest.main()
