from __future__ import annotations

import asyncio
import inspect
import unittest
from datetime import datetime
from uuid import uuid4

from sqlalchemy import inspect as sqlalchemy_inspect, text

from src.core.enums import MessageRole, TaskStatus
from src.core.models import Conversation, Message, Task, TaskInputAttachment
from src.state.postgres.worker import postgres_test_dsn_or_skip_reason
from src.storage.postgres import bootstrap_postgres_database, create_postgres_engine, create_postgres_session_factory
from src.storage.postgres.repositories import PostgreSQLStorage


class PostgresConversationDeleteTest(unittest.TestCase):
    def test_physical_delete_uses_set_based_sql_not_python_id_materialization(self) -> None:
        source = inspect.getsource(PostgreSQLStorage._delete_conversation_physical_sync)
        self.assertIn("DELETE FROM artifact", source)
        self.assertIn("DELETE FROM task_input_attachment", source)
        self.assertIn("USING task", source)
        self.assertIn("DELETE FROM mailbox_delivery", source)
        self.assertIn("USING mailbox_message", source)
        self.assertIn("SELECT task_id FROM task WHERE conversation_id", source)
        self.assertNotIn("task_ids =", source)
        self.assertNotIn("mailbox_message_ids =", source)
        self.assertNotIn("interrupt_ids =", source)

    def test_mark_deleting_uses_row_lock_for_cross_process_claim(self) -> None:
        source = inspect.getsource(PostgreSQLStorage._mark_conversation_deleting_sync)
        self.assertIn("with_for_update", source)
        self.assertIn("ConversationStatus.ACTIVE", source)
        self.assertIn("ConversationStatus.DELETING", source)

    def test_real_postgres_bootstrap_and_delete_task_input_attachment_when_dsn_configured(self) -> None:
        dsn, skip_reason = postgres_test_dsn_or_skip_reason()
        if skip_reason:
            self.skipTest(skip_reason)
        self.assertIsNotNone(dsn)
        engine = create_postgres_engine(str(dsn))
        try:
            bootstrap_postgres_database(engine)
            inspector = sqlalchemy_inspect(engine)
            self.assertTrue(inspector.has_table("task_input_attachment"))
            self.assertLessEqual(
                {
                    "idx_task_input_attachment_task_created",
                    "idx_task_input_attachment_conversation_task",
                    "idx_task_input_attachment_upload",
                },
                {str(index["name"]) for index in inspector.get_indexes("task_input_attachment")},
            )

            suffix = uuid4().hex
            conversation_id = f"conv-delete-{suffix}"
            task_id = f"task-delete-{suffix}"
            message_id = f"msg-delete-{suffix}"
            attachment_id = f"attachment-delete-{suffix}"
            now = datetime(2026, 6, 3, 12, 0, 0)
            storage = PostgreSQLStorage(create_postgres_session_factory(engine))
            asyncio.run(
                storage.save_conversation(
                    Conversation(
                        conversation_id=conversation_id,
                        username="postgres-contract",
                        current_task_id=task_id,
                        created_at=now,
                        updated_at=now,
                    )
                )
            )
            asyncio.run(
                storage.save_message(
                    Message(
                        message_id=message_id,
                        conversation_id=conversation_id,
                        role=MessageRole.USER,
                        content="上传材料",
                        task_id=task_id,
                        created_at=now,
                    )
                )
            )
            asyncio.run(
                storage.save_task(
                    Task(
                        task_id=task_id,
                        conversation_id=conversation_id,
                        root_message_id=message_id,
                        status=TaskStatus.RUNNING,
                        created_at=now,
                        updated_at=now,
                    )
                )
            )
            asyncio.run(
                storage.save_task_input_attachment(
                    TaskInputAttachment(
                        attachment_id=attachment_id,
                        task_id=task_id,
                        conversation_id=conversation_id,
                        source_kind="message_upload",
                        source_upload_id=f"upl-{suffix}",
                        source_message_id=message_id,
                        filename="materials.csv",
                        content_type="text/csv",
                        file_type="csv",
                        size_bytes=16,
                        sha256="sha-postgres-contract",
                        prompt_artifact={"upload_id": f"upl-{suffix}", "filename": "materials.csv"},
                        skill_artifact={
                        "upload_id": f"upl-{suffix}",
                        "filename": "materials.csv",
                        "content": "ped_id\nA001\n",
                    },
                        source_payload={"encoding": "base64", "content_base64": "cGVkX2lkCkEwMDEK"},
                        created_at=now,
                        updated_at=now,
                    )
                )
            )

            with engine.connect() as connection:
                before = connection.execute(
                    text("SELECT count(*) FROM task_input_attachment WHERE attachment_id = :attachment_id"),
                    {"attachment_id": attachment_id},
                ).scalar_one()
            self.assertEqual(before, 1)

            deleted_counts = asyncio.run(storage.delete_conversation(conversation_id))
            self.assertGreaterEqual(deleted_counts["task_input_attachment"], 1)

            with engine.connect() as connection:
                after = connection.execute(
                    text("SELECT count(*) FROM task_input_attachment WHERE attachment_id = :attachment_id"),
                    {"attachment_id": attachment_id},
                ).scalar_one()
            self.assertEqual(after, 0)
        finally:
            engine.dispose()
