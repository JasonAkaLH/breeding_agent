from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import delete

from src.core.models import Conversation, ConversationFileResource
from src.storage.conversation_files import (
    build_file_upload_message_projection,
    file_upload_message_id,
)
from src.storage.postgres import (
    PostgreSQLStorage,
    bootstrap_postgres_database,
    create_postgres_engine,
    create_postgres_session_factory,
)
from src.storage.sqlalchemy_models import (
    ConversationFileResourceRow,
    ConversationRow,
    EventRecordRow,
    MessageRow,
)
from tests.postgres_test_support import isolated_postgres_test_dsn_or_skip_reason


class ConversationFileSheetSelectionPostgresIntegrationTest(
    unittest.IsolatedAsyncioTestCase
):
    @classmethod
    def setUpClass(cls) -> None:
        dsn, skip_reason = isolated_postgres_test_dsn_or_skip_reason(
            "MAF_POSTGRES_CONVERSATION_FILE_TEST_DSN",
            fallback_env="MAF_POSTGRES_TEST_DSN",
        )
        if skip_reason:
            raise unittest.SkipTest(skip_reason)
        assert dsn is not None
        cls.engine = create_postgres_engine(dsn, pool_size=4, max_overflow=0)
        bootstrap_postgres_database(cls.engine)
        cls.session_factory = create_postgres_session_factory(cls.engine)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.dispose()

    async def asyncSetUp(self) -> None:
        suffix = uuid4().hex
        self.conversation_id = f"sheet-selection-pg-conversation-{suffix}"
        self.upload_id = f"sheet-selection-pg-upload-{suffix}"
        self.username = f"sheet-selection-pg-owner-{suffix}"
        self.now = datetime(2026, 8, 27, 8, 0)
        self.storage = PostgreSQLStorage(self.session_factory)
        await self.storage.save_conversation(
            Conversation(
                conversation_id=self.conversation_id,
                username=self.username,
                created_at=self.now,
                updated_at=self.now,
            )
        )

    async def asyncTearDown(self) -> None:
        with self.session_factory() as session:
            session.execute(
                delete(EventRecordRow).where(
                    EventRecordRow.conversation_id == self.conversation_id
                )
            )
            session.execute(
                delete(MessageRow).where(
                    MessageRow.conversation_id == self.conversation_id
                )
            )
            session.execute(
                delete(ConversationFileResourceRow).where(
                    ConversationFileResourceRow.conversation_id
                    == self.conversation_id
                )
            )
            session.execute(
                delete(ConversationRow).where(
                    ConversationRow.conversation_id == self.conversation_id
                )
            )
            session.commit()

    async def test_deleted_upload_conflicts_without_resource_or_message_resurrection(self) -> None:
        resource = ConversationFileResource(
            file_id=self.upload_id,
            conversation_id=self.conversation_id,
            username=self.username,
            original_filename="materials.xlsx",
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            file_type="spreadsheet",
            size_bytes=12,
            sha256="a" * 64,
            storage_key=f"{self.conversation_id}/{self.upload_id}/original",
            preview={"excel_sheets": [{"sheet_name": "Alpha"}, {"sheet_name": "Beta"}]},
            requires_sheet_selection=True,
            created_at=self.now,
            updated_at=self.now,
        )
        await self.storage.save_conversation_file_resource_with_upload_message(
            resource,
            build_file_upload_message_projection(resource),
            now=self.now,
        )
        deleted_at = self.now + timedelta(seconds=1)
        await self.storage.mark_conversation_file_resource_and_upload_message_deleted(
            self.conversation_id,
            self.username,
            self.upload_id,
            updated_at=deleted_at,
        )
        selected = replace(
            resource,
            selected_sheet="Beta",
            requires_sheet_selection=False,
            updated_at=deleted_at + timedelta(seconds=1),
        )

        with self.assertRaisesRegex(
            RuntimeError, "conversation_file_sheet_selection_conflict"
        ):
            await self.storage.apply_conversation_file_sheet_selection_exact(
                resource,
                selected,
            )

        stored = await self.storage.get_conversation_file_resource_by_id(
            self.upload_id
        )
        message = await self.storage.get_message(
            file_upload_message_id(self.upload_id)
        )
        self.assertEqual(stored.status, "deleted")
        self.assertIsNone(stored.selected_sheet)
        self.assertEqual(message.metadata["file_status"], "deleted")
