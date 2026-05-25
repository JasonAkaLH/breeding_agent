from __future__ import annotations

from datetime import datetime

from sqlalchemy import inspect

from src.core.enums import ConversationStatus
from src.core.models import Conversation, ConversationMemorySummary, PendingSkillContext
from src.storage.sqlite import bootstrap_sqlite_database, create_sqlite_engine
from src.storage.sqlite.repositories import SQLiteStateRepository
from tests.storage.support import SQLiteStorageTestCase


class SQLiteUsernameMigrationTest(SQLiteStorageTestCase):
    def test_existing_account_id_columns_are_backfilled_to_username_columns(self) -> None:
        self.engine.dispose()
        self.db_path.unlink()
        engine = create_sqlite_engine(self.db_path)
        try:
            with engine.begin() as connection:
                connection.exec_driver_sql(
                    """
                    CREATE TABLE conversation (
                      conversation_id TEXT PRIMARY KEY,
                      account_id TEXT NOT NULL,
                      status TEXT NOT NULL,
                      current_task_id TEXT NULL,
                      title TEXT NULL,
                      created_at TEXT NULL,
                      updated_at TEXT NULL
                    )
                    """
                )
                connection.exec_driver_sql(
                    """
                    CREATE TABLE conversation_memory_summary (
                      summary_id TEXT PRIMARY KEY,
                      conversation_id TEXT NOT NULL,
                      account_id TEXT NOT NULL,
                      covered_until_turn_id TEXT NULL,
                      covered_until_message_id TEXT NULL,
                      covered_until_created_at TEXT NULL,
                      summary_text TEXT NOT NULL,
                      source_message_count INTEGER NOT NULL,
                      source_message_ids_hash TEXT NOT NULL,
                      estimated_tokens INTEGER NOT NULL,
                      summary_version TEXT NOT NULL,
                      compression_policy_version TEXT NOT NULL,
                      model_metadata_safe TEXT NULL,
                      last_error TEXT NULL,
                      created_at TEXT NULL,
                      updated_at TEXT NULL
                    )
                    """
                )
                connection.exec_driver_sql(
                    """
                    CREATE TABLE conversation_pending_skill_context (
                      context_id TEXT PRIMARY KEY,
                      conversation_id TEXT NOT NULL,
                      account_id TEXT NULL,
                      capability_id TEXT NOT NULL,
                      skill_name TEXT NOT NULL,
                      source_task_id TEXT NOT NULL,
                      source_message_id TEXT NOT NULL,
                      original_user_message TEXT NOT NULL,
                      missing_requirements TEXT NULL,
                      assistant_message TEXT NOT NULL,
                      status TEXT NOT NULL,
                      created_at TEXT NULL,
                      updated_at TEXT NULL
                    )
                    """
                )
                connection.exec_driver_sql("INSERT INTO conversation (conversation_id, account_id, status) VALUES ('conv-old', 'alice', 'active')")
                connection.exec_driver_sql(
                    """
                    INSERT INTO conversation_memory_summary (
                      summary_id, conversation_id, account_id, summary_text, source_message_count,
                      source_message_ids_hash, estimated_tokens, summary_version, compression_policy_version
                    ) VALUES ('sum-old', 'conv-old', 'alice', 'summary', 1, 'hash', 10, 'v1', 'cp1')
                    """
                )
                connection.exec_driver_sql(
                    """
                    INSERT INTO conversation_pending_skill_context (
                      context_id, conversation_id, account_id, capability_id, skill_name, source_task_id,
                      source_message_id, original_user_message, assistant_message, status
                    ) VALUES ('ctx-old', 'conv-old', 'alice', 'skill.example', 'example', 'task-1', 'msg-1', 'question', 'answer', 'pending_user_input')
                    """
                )

            bootstrap_sqlite_database(engine)
            inspector = inspect(engine)
            for table in ("conversation", "conversation_memory_summary", "conversation_pending_skill_context"):
                columns = {column["name"] for column in inspector.get_columns(table)}
                self.assertIn("username", columns, table)
                self.assertNotIn("account_id", columns, table)

            with engine.connect() as connection:
                self.assertEqual(connection.exec_driver_sql("SELECT username FROM conversation WHERE conversation_id='conv-old'").scalar_one(), "alice")
                self.assertEqual(connection.exec_driver_sql("SELECT username FROM conversation_memory_summary WHERE summary_id='sum-old'").scalar_one(), "alice")
                self.assertEqual(connection.exec_driver_sql("SELECT username FROM conversation_pending_skill_context WHERE context_id='ctx-old'").scalar_one(), "alice")
        finally:
            engine.dispose()
            self.engine = create_sqlite_engine(self.db_path)
            self.session_factory.configure(bind=self.engine)

    def test_legacy_owner_tables_accept_new_username_writes_after_bootstrap(self) -> None:
        self.engine.dispose()
        self.db_path.unlink()
        engine = create_sqlite_engine(self.db_path)
        try:
            with engine.begin() as connection:
                connection.exec_driver_sql(
                    """
                    CREATE TABLE conversation (
                      conversation_id TEXT PRIMARY KEY,
                      account_id TEXT NOT NULL,
                      status TEXT NOT NULL,
                      current_task_id TEXT NULL,
                      title TEXT NULL,
                      created_at TEXT NULL,
                      updated_at TEXT NULL
                    )
                    """
                )
                connection.exec_driver_sql(
                    """
                    CREATE TABLE conversation_memory_summary (
                      summary_id TEXT PRIMARY KEY,
                      conversation_id TEXT NOT NULL,
                      account_id TEXT NOT NULL,
                      covered_until_turn_id TEXT NULL,
                      covered_until_message_id TEXT NULL,
                      covered_until_created_at TEXT NULL,
                      summary_text TEXT NOT NULL,
                      source_message_count INTEGER NOT NULL,
                      source_message_ids_hash TEXT NOT NULL,
                      estimated_tokens INTEGER NOT NULL,
                      summary_version TEXT NOT NULL,
                      compression_policy_version TEXT NOT NULL,
                      model_metadata_safe TEXT NULL,
                      last_error TEXT NULL,
                      created_at TEXT NULL,
                      updated_at TEXT NULL
                    )
                    """
                )
                connection.exec_driver_sql(
                    """
                    CREATE TABLE conversation_pending_skill_context (
                      context_id TEXT PRIMARY KEY,
                      conversation_id TEXT NOT NULL,
                      account_id TEXT NULL,
                      capability_id TEXT NOT NULL,
                      skill_name TEXT NOT NULL,
                      source_task_id TEXT NOT NULL,
                      source_message_id TEXT NOT NULL,
                      original_user_message TEXT NOT NULL,
                      missing_requirements TEXT NULL,
                      assistant_message TEXT NOT NULL,
                      status TEXT NOT NULL,
                      created_at TEXT NULL,
                      updated_at TEXT NULL
                    )
                    """
                )

            bootstrap_sqlite_database(engine)
            self.session_factory.configure(bind=engine)
            now = datetime(2026, 5, 25, 12, 0, 0)
            conversation = Conversation(
                conversation_id="conv-new",
                username="bob",
                status=ConversationStatus.ACTIVE,
                created_at=now,
                updated_at=now,
            )
            summary = ConversationMemorySummary(
                summary_id="sum-new",
                conversation_id="conv-new",
                username="bob",
                covered_until_turn_id="task-1",
                covered_until_message_id="msg-1",
                covered_until_created_at=now,
                summary_text="summary",
                source_message_count=1,
                source_message_ids_hash="hash",
                estimated_tokens=5,
                summary_version="v1",
                compression_policy_version="cp1",
                created_at=now,
                updated_at=now,
            )
            context = PendingSkillContext(
                context_id="ctx-new",
                conversation_id="conv-new",
                username="bob",
                capability_id="skill.example",
                skill_name="example",
                source_task_id="task-1",
                source_message_id="msg-1",
                original_user_message="question",
                missing_requirements=("variety",),
                assistant_message="answer",
                created_at=now,
                updated_at=now,
            )

            with self.session_factory() as session:
                repo = SQLiteStateRepository(session)
                repo.save_conversation(conversation)
                repo.save_conversation_memory_summary(summary)
                repo.save_pending_skill_context(context)
                session.commit()

            with self.session_factory() as session:
                repo = SQLiteStateRepository(session)
                self.assertEqual(repo.get_conversation("conv-new"), conversation)
                self.assertEqual(repo.get_conversation_memory_summary("sum-new"), summary)
                self.assertEqual(repo.get_pending_skill_context("ctx-new"), context)
        finally:
            engine.dispose()
            self.engine = create_sqlite_engine(self.db_path)
            self.session_factory.configure(bind=self.engine)

    def test_existing_username_column_is_backfilled_when_legacy_account_id_remains(self) -> None:
        self.engine.dispose()
        self.db_path.unlink()
        engine = create_sqlite_engine(self.db_path)
        try:
            with engine.begin() as connection:
                connection.exec_driver_sql(
                    """
                    CREATE TABLE conversation (
                      conversation_id TEXT PRIMARY KEY,
                      account_id TEXT NOT NULL,
                      username TEXT NULL,
                      status TEXT NOT NULL,
                      current_task_id TEXT NULL,
                      title TEXT NULL,
                      created_at TEXT NULL,
                      updated_at TEXT NULL
                    )
                    """
                )
                connection.exec_driver_sql(
                    "INSERT INTO conversation (conversation_id, account_id, username, status) VALUES ('conv-partial', 'alice', NULL, 'active')"
                )

            bootstrap_sqlite_database(engine)

            with engine.connect() as connection:
                self.assertEqual(
                    connection.exec_driver_sql(
                        "SELECT username FROM conversation WHERE conversation_id='conv-partial'"
                    ).scalar_one(),
                    "alice",
                )
        finally:
            engine.dispose()
            self.engine = create_sqlite_engine(self.db_path)
            self.session_factory.configure(bind=self.engine)
