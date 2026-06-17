from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from src.core.models import ConversationFileResource
from src.storage.conversation_files import ConversationFileIndexWriter, LocalConversationFileStore
from src.storage.sqlite import SQLiteStorage
from src.storage.sqlite.models import ConversationFileResourceRow
from src.storage.sqlite.repositories import SQLiteStateRepository
from tests.storage.support import SQLiteStorageTestCase


class ConversationFileResourceRepositoryTest(SQLiteStorageTestCase):
    def test_bootstrap_creates_conversation_file_resource_table_and_indexes(self) -> None:
        inspector = inspect(self.engine)
        self.assertIn("conversation_file_resource", set(inspector.get_table_names()))
        index_names = {index["name"] for index in inspector.get_indexes("conversation_file_resource")}
        self.assertIn("idx_conversation_file_conversation_status_created", index_names)
        self.assertIn("idx_conversation_file_username_conversation", index_names)

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
