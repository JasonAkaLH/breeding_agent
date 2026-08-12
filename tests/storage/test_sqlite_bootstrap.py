from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from sqlalchemy import inspect, text

from src.core.models import AuthUserToken, Conversation
from src.core.contracts import StoragePort as CoreStoragePort
from src.storage.interfaces import StoragePort
from src.storage.sqlite import SQLiteStorage, bootstrap_sqlite_database, create_sqlite_engine, create_sqlite_session_factory
from src.storage.sqlite.repositories import SQLiteStateRepository
from tests.storage.support import SQLiteStorageTestCase


class SQLiteBootstrapTest(SQLiteStorageTestCase):
    def test_storage_interface_reexports_canonical_protocol(self) -> None:
        self.assertIs(StoragePort, CoreStoragePort)

    def test_bootstrap_creates_phase2_core_tables(self) -> None:
        table_names = set(inspect(self.engine).get_table_names())
        self.assertTrue(
            {
                "conversation",
                "conversation_pending_skill_context",
                "message",
                "task",
                "task_node",
                "task_edge",
                "artifact",
                "task_input_attachment",
                "event_record",
                "mailbox_message",
                "mailbox_delivery",
                "interrupt",
                "interrupt_answer",
                "checkpoint",
                "user_mcp_server",
                "user_mcp_tool_grant",
                "user_mcp_health_attempt",
                "user_mcp_scope_lease",
                "mcp_credential_key_validation",
            }.issubset(table_names)
        )

    def test_bootstrap_drops_legacy_auth_tables_and_keeps_current_token_table(self) -> None:
        legacy_tables = {"auth_user", "auth_captcha_challenge", "auth_session", "auth_api_token"}
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE auth_user ("
                    "username TEXT PRIMARY KEY, password_hash TEXT, password_salt TEXT, "
                    "password_scheme TEXT, status TEXT)"
                )
            )
            connection.execute(
                text(
                    "CREATE TABLE auth_captcha_challenge ("
                    "captcha_id TEXT PRIMARY KEY, code_hash TEXT, expires_at TEXT)"
                )
            )
            connection.execute(
                text(
                    "CREATE TABLE auth_session ("
                    "session_id TEXT PRIMARY KEY, username TEXT, expires_at TEXT)"
                )
            )
            connection.execute(
                text(
                    "CREATE TABLE auth_api_token ("
                    "token_id TEXT PRIMARY KEY, token_hash TEXT, username TEXT)"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO auth_user "
                    "(username, password_hash, password_salt, password_scheme, status) "
                    "VALUES ('alice', 'hash', 'salt', 'scheme', 'active')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO auth_captcha_challenge (captcha_id, code_hash, expires_at) "
                    "VALUES ('captcha-1', 'hash', '2026-05-25T12:00:00')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO auth_session (session_id, username, expires_at) "
                    "VALUES ('session-1', 'alice', '2026-05-25T12:00:00')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO auth_api_token (token_id, token_hash, username) "
                    "VALUES ('token-1', 'hash', 'alice')"
                )
            )

        bootstrap_sqlite_database(self.engine)

        table_names = set(inspect(self.engine).get_table_names())
        self.assertFalse(legacy_tables.intersection(table_names))
        self.assertIn("auth_user_token", table_names)

        with self.session_factory() as session:
            repo = SQLiteStateRepository(session)
            saved = repo.save_auth_user_token(AuthUserToken(username="alice", api_token_hash="current-hash"))
            session.commit()
        self.assertEqual(saved.username, "alice")

    def test_bootstrap_does_not_create_legacy_auth_tables_when_missing(self) -> None:
        bootstrap_sqlite_database(self.engine)
        table_names = set(inspect(self.engine).get_table_names())
        self.assertFalse(
            {"auth_user", "auth_captcha_challenge", "auth_session", "auth_api_token"}.intersection(
                table_names
            )
        )
        self.assertIn("auth_user_token", table_names)

    def test_sqlite_storage_implements_storage_port(self) -> None:
        self.assertIsInstance(SQLiteStorage(self.session_factory), StoragePort)

    def test_sqlite_storage_async_facade_round_trip(self) -> None:
        storage = SQLiteStorage(self.session_factory)
        conversation = Conversation(conversation_id="conv-async", username="acc-async")

        saved = asyncio.run(storage.save_conversation(conversation))
        loaded = asyncio.run(storage.get_conversation("conv-async"))

        self.assertEqual(saved, conversation)
        self.assertEqual(loaded, conversation)

    def test_bootstrap_migrates_legacy_message_rows_with_public_history_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            engine = create_sqlite_engine(Path(tmpdir) / "legacy-message.sqlite3")
            try:
                session_factory = create_sqlite_session_factory(engine)
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            "CREATE TABLE message ("
                            "message_id TEXT PRIMARY KEY, "
                            "conversation_id TEXT NOT NULL, "
                            "role TEXT NOT NULL, "
                            "content TEXT NOT NULL, "
                            "task_id TEXT, "
                            "stream_status TEXT, "
                            "created_at TEXT)"
                        )
                    )
                    connection.execute(
                        text(
                            "INSERT INTO message "
                            "(message_id, conversation_id, role, content, task_id, stream_status, created_at) "
                            "VALUES ('msg-legacy', 'conv-legacy', 'user', 'hello', NULL, NULL, '2026-06-16T07:00:00')"
                        )
                    )

                bootstrap_sqlite_database(engine)
                storage = SQLiteStorage(session_factory)
                loaded = asyncio.run(storage.get_message("msg-legacy"))

                self.assertEqual(loaded.message_type, "chat")
                self.assertEqual(loaded.metadata, {})
                self.assertIsNone(loaded.updated_at)
                self.assertEqual(loaded.content, "hello")
            finally:
                engine.dispose()
