from __future__ import annotations

import asyncio
import tempfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from src.core.models import AuthUserToken, Conversation, MCPRolloutMetricBucket
from src.core.contracts import StoragePort as CoreStoragePort
from src.storage.interfaces import StoragePort
from src.storage.sqlite import SQLiteStorage, bootstrap_sqlite_database, create_sqlite_engine, create_sqlite_session_factory
from src.storage.sqlite.base import SQLiteBase
from src.storage.sqlite.repositories import SQLiteStateRepository
from tests.storage.support import SQLiteStorageTestCase


class SQLiteBootstrapTest(SQLiteStorageTestCase):
    def test_result_authority_columns_are_added_without_rebuilding_business_calls(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            engine = create_sqlite_engine(Path(tmpdir) / "result-authority.sqlite3")
            self.addCleanup(engine.dispose)
            SQLiteBase.metadata.tables["mcp_call_record"].create(engine)
            with engine.begin() as connection:
                for column in (
                    "output_schema",
                    "output_schema_sha256",
                    "terminal_result_source",
                ):
                    connection.execute(
                        text(f'ALTER TABLE mcp_call_record DROP COLUMN "{column}"')
                    )
                connection.execute(
                    text(
                        "INSERT INTO mcp_call_record ("
                        "call_ref, branch_id, owner_user_id, task_id, node_id, "
                        "server_id, tool_name, status, call_sequence, arguments_sha256, "
                        "server_security_version, input_schema_sha256, may_have_dispatched"
                        ") VALUES ("
                        "'call-existing', 'branch-1', 'alice', 'task-1', 'node-1', "
                        "'server-1', 'lookup', 'reserved', 1, 'sha256:arguments', "
                        "1, 'sha256:input', 0)"
                    )
                )

            bootstrap_sqlite_database(engine)

            columns = {
                column["name"]
                for column in inspect(engine).get_columns("mcp_call_record")
            }
            self.assertTrue(
                {
                    "output_schema",
                    "output_schema_sha256",
                    "terminal_result_source",
                }.issubset(columns)
            )
            with engine.connect() as connection:
                row = connection.execute(
                    text(
                        "SELECT call_ref, output_schema, output_schema_sha256, "
                        "terminal_result_source FROM mcp_call_record"
                    )
                ).one()
            self.assertEqual(row[0], "call-existing")
            self.assertEqual(tuple(row[1:]), (None, None, None))

    def test_legacy_dispatch_outbox_with_business_row_requires_operator_migration(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            engine = create_sqlite_engine(Path(tmpdir) / "legacy-dispatch.sqlite3")
            self.addCleanup(engine.dispose)
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "CREATE TABLE mcp_dispatch_resume_outbox ("
                        "outbox_id TEXT PRIMARY KEY, intent_id TEXT NOT NULL, "
                        "status TEXT NOT NULL)"
                    )
                )
                connection.execute(
                    text(
                        "INSERT INTO mcp_dispatch_resume_outbox "
                        "VALUES ('outbox-1', 'intent-1', 'claimed')"
                    )
                )

            with self.assertRaisesRegex(
                RuntimeError, "mcp_dispatch_aggregate_migration_required"
            ):
                bootstrap_sqlite_database(engine)

            self.assertEqual(
                {
                    column["name"]
                    for column in inspect(engine).get_columns(
                        "mcp_dispatch_resume_outbox"
                    )
                },
                {"outbox_id", "intent_id", "status"},
            )

    def test_empty_legacy_dispatch_tables_are_rebuilt_to_fresh_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            engine = create_sqlite_engine(Path(tmpdir) / "empty-dispatch.sqlite3")
            self.addCleanup(engine.dispose)
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "CREATE TABLE mcp_dispatch_resume_outbox ("
                        "outbox_id TEXT PRIMARY KEY, intent_id TEXT NOT NULL, "
                        "status TEXT NOT NULL)"
                    )
                )
                connection.execute(
                    text(
                        "CREATE TABLE mcp_call_record ("
                        "call_ref TEXT PRIMARY KEY, status TEXT NOT NULL)"
                    )
                )

            bootstrap_sqlite_database(engine)

            outbox_columns = {
                column["name"]
                for column in inspect(engine).get_columns(
                    "mcp_dispatch_resume_outbox"
                )
            }
            call_columns = {
                column["name"]
                for column in inspect(engine).get_columns("mcp_call_record")
            }
            self.assertIn("resume_reason", outbox_columns)
            self.assertIn("selector_step_total", outbox_columns)
            self.assertIn("pending_action_id", call_columns)
            self.assertIn("continuation_of_call_ref", call_columns)

    def test_legacy_terminal_receipt_adds_v2_result_identity_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            engine = create_sqlite_engine(Path(tmpdir) / "legacy-receipt.sqlite3")
            self.addCleanup(engine.dispose)
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "CREATE TABLE mcp_terminal_result_receipt ("
                        "result_receipt_id TEXT PRIMARY KEY)"
                    )
                )

            bootstrap_sqlite_database(engine)

            columns = {
                column["name"]
                for column in inspect(engine).get_columns(
                    "mcp_terminal_result_receipt"
                )
            }
            self.assertTrue(
                {
                    "safe_result_content_sha256",
                    "safe_result_size_bytes",
                    "safe_result_store_kind",
                }.issubset(columns)
            )

    def test_legacy_remote_task_publication_columns_are_added_and_proven_rows_backfilled(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            engine = create_sqlite_engine(Path(tmpdir) / "legacy-remote-task.sqlite3")
            self.addCleanup(engine.dispose)
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "CREATE TABLE mcp_remote_task_binding ("
                        "safe_remote_task_ref TEXT PRIMARY KEY, owner_user_id TEXT NOT NULL, "
                        "task_id TEXT NOT NULL, node_id TEXT NOT NULL, call_ref TEXT NOT NULL, "
                        "server_id TEXT NOT NULL, protocol_version TEXT NOT NULL, "
                        "remote_task_ciphertext BLOB NOT NULL, remote_task_nonce BLOB NOT NULL, "
                        "encryption_version INTEGER NOT NULL, last_status TEXT NOT NULL, "
                        "next_poll_at TEXT, created_at TEXT, updated_at TEXT, terminal_at TEXT, "
                        "claim_owner TEXT, claim_token TEXT, lease_expires_at TEXT, revision INTEGER DEFAULT 0)"
                    )
                )
                connection.execute(
                    text(
                        "INSERT INTO mcp_remote_task_binding VALUES "
                        "('published','alice','task-a','node-a','call-a','server','2026-07-28',"
                        "X'01',X'02',1,'working','2026-08-13 08:00:00',NULL,'2026-08-13 07:59:00',NULL,NULL,NULL,NULL,0),"
                        "('unpublished','alice','task-b','node-b','call-b','server','2026-07-28',"
                        "X'01',X'02',1,'working',NULL,NULL,'2026-08-13 07:59:00',NULL,NULL,NULL,NULL,0)"
                    )
                )

            bootstrap_sqlite_database(engine)

            columns = {
                column["name"]
                for column in inspect(engine).get_columns("mcp_remote_task_binding")
            }
            self.assertIn("published_at", columns)
            self.assertIn("continuation_plan", columns)
            with engine.connect() as connection:
                rows = connection.execute(
                    text(
                        "SELECT safe_remote_task_ref, published_at "
                        "FROM mcp_remote_task_binding ORDER BY safe_remote_task_ref"
                    )
                ).all()
            self.assertEqual(rows[0], ("published", "2026-08-13 08:00:00"))
            self.assertEqual(rows[1], ("unpublished", None))
            storage = SQLiteStorage(create_sqlite_session_factory(engine))
            self.assertIsNotNone(
                asyncio.run(
                    storage.get_mcp_remote_task_binding("alice", "task-a", "published")
                )
            )

    def test_bootstrap_expands_task_route_reason_constraint(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            engine = create_sqlite_engine(Path(temp_dir) / "legacy-task-reasons.db")
            self.addCleanup(engine.dispose)
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "CREATE TABLE task ("
                        "task_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, "
                        "root_message_id TEXT NOT NULL, status TEXT NOT NULL, "
                        "routing_mode TEXT NOT NULL, requested_capability_id TEXT, "
                        "root_node_id TEXT, summary TEXT, cancel_requested_at TEXT, "
                        "created_at TEXT, updated_at TEXT, mcp_execution_mode TEXT, "
                        "mcp_shadow_enabled BOOLEAN, mcp_rollout_config_version TEXT, "
                        "mcp_route_reason_code TEXT CHECK (mcp_route_reason_code IS NULL OR "
                        "mcp_route_reason_code IN ('routing_off', 'shadow_enabled', "
                        "'enforce_selected', 'cohort_not_selected', "
                        "'percent_not_selected', 'no_execution_path')), "
                        "mcp_rollout_mode TEXT)"
                    )
                )
                connection.execute(
                    text(
                        "INSERT INTO task (task_id, conversation_id, root_message_id, "
                        "status, routing_mode, mcp_execution_mode, mcp_shadow_enabled, "
                        "mcp_rollout_config_version, mcp_route_reason_code, mcp_rollout_mode) "
                        "VALUES ('task-history', 'conv-history', 'msg-history', "
                        "'completed', 'auto', 'legacy', 0, 'v1', 'routing_off', 'off')"
                    )
                )

            bootstrap_sqlite_database(engine)

            with engine.begin() as connection:
                self.assertEqual(
                    connection.execute(
                        text(
                            "SELECT mcp_route_reason_code FROM task "
                            "WHERE task_id = 'task-history'"
                        )
                    ).scalar_one(),
                    "routing_off",
                )
                connection.execute(
                    text(
                        "INSERT INTO task (task_id, conversation_id, root_message_id, "
                        "status, routing_mode, mcp_execution_mode, mcp_shadow_enabled, "
                        "mcp_rollout_config_version, mcp_route_reason_code, mcp_rollout_mode) "
                        "VALUES ('task-new', 'conv-new', 'msg-new', 'accepted', 'auto', "
                        "'unavailable', 0, 'v2', 'no_user_scoped_server', 'off')"
                    )
                )
            with self.assertRaises(IntegrityError), engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO task (task_id, conversation_id, root_message_id, "
                        "status, routing_mode, mcp_execution_mode, mcp_shadow_enabled, "
                        "mcp_rollout_config_version, mcp_route_reason_code, mcp_rollout_mode) "
                        "VALUES ('task-invalid', 'conv-invalid', 'msg-invalid', "
                        "'accepted', 'auto', 'unavailable', 0, 'v2', "
                        "'NO_USER_SCOPED_SERVER', 'off')"
                    )
                )

    def test_bootstrap_expands_legacy_rollout_block_reason_constraint(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            engine = create_sqlite_engine(Path(temp_dir) / "legacy-block.db")
            self.addCleanup(engine.dispose)
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "CREATE TABLE mcp_rollout_promotion_block ("
                        "block_id TEXT PRIMARY KEY, environment_id TEXT NOT NULL, "
                        "rollout_program TEXT NOT NULL, deployment_id TEXT NOT NULL, "
                        "stage TEXT NOT NULL, config_fingerprint TEXT NOT NULL, "
                        "evidence_id TEXT NOT NULL, reason_code TEXT NOT NULL "
                        "CHECK (reason_code IN ('digest_invalid')), created_at TEXT NOT NULL)"
                    )
                )
                connection.execute(
                    text(
                        "INSERT INTO mcp_rollout_promotion_block VALUES ("
                        "'legacy-block', 'staging', 'user_mcp_phase3', 'deploy-a', "
                        "'internal_shadow', :fingerprint, 'evidence-a', "
                        "'digest_invalid', :created_at)"
                    ),
                    {
                        "fingerprint": "b" * 64,
                        "created_at": "2026-08-13T01:00:01Z",
                    },
                )

            bootstrap_sqlite_database(engine)

            with engine.begin() as connection:
                self.assertEqual(
                    connection.execute(
                        text(
                            "SELECT reason_code FROM mcp_rollout_promotion_block "
                            "WHERE block_id = 'legacy-block'"
                        )
                    ).scalar_one(),
                    "digest_invalid",
                )
                connection.execute(
                    text(
                        "INSERT INTO mcp_rollout_promotion_block VALUES ("
                        "'new-block', 'staging', 'user_mcp_phase3', 'deploy-a', "
                        "'internal_shadow', :fingerprint, 'evidence-b', "
                        "'attestation_invalid', :created_at)"
                    ),
                    {
                        "fingerprint": "b" * 64,
                        "created_at": "2026-08-13T01:00:02Z",
                    },
                )
                connection.execute(
                    text(
                        "INSERT INTO mcp_rollout_promotion_block VALUES ("
                        "'red-line-block', 'staging', 'user_mcp_phase3', 'deploy-a', "
                        "'internal_shadow', :fingerprint, 'evidence-c', "
                        "'safety_red_line_nonzero', :created_at)"
                    ),
                    {
                        "fingerprint": "b" * 64,
                        "created_at": "2026-08-13T01:00:03Z",
                    },
                )

    def test_bootstrap_adds_nullable_attestation_columns_to_legacy_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            engine = create_sqlite_engine(Path(temp_dir) / "legacy-evidence.db")
            self.addCleanup(engine.dispose)
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "CREATE TABLE mcp_rollout_evidence_snapshot ("
                        "evidence_id TEXT PRIMARY KEY, environment_id TEXT NOT NULL, "
                        "rollout_program TEXT NOT NULL, git_sha TEXT NOT NULL, "
                        "deployment_id TEXT NOT NULL, stage TEXT NOT NULL, "
                        "config_fingerprint TEXT NOT NULL, window_started_at TEXT NOT NULL, "
                        "window_ended_at TEXT NOT NULL, recorded_at TEXT NOT NULL, "
                        "producer TEXT NOT NULL, source TEXT NOT NULL, snapshot_id INTEGER NOT NULL, "
                        "nonce TEXT NOT NULL, evidence_kind TEXT NOT NULL, payload TEXT NOT NULL, "
                        "payload_digest TEXT NOT NULL)"
                    )
                )
                connection.execute(
                    text(
                        "INSERT INTO mcp_rollout_evidence_snapshot VALUES ("
                        "'legacy-prod', 'staging', 'user_mcp_phase3', :git_sha, "
                        "'deploy-a', 'internal_shadow', :fingerprint, :started, :ended, "
                        ":recorded, 'production_snapshot_producer', 'production', 1, "
                        "'legacy-nonce', 'internal_shadow', '{}', :digest)"
                    ),
                    {
                        "git_sha": "a" * 40,
                        "fingerprint": "b" * 64,
                        "started": "2026-08-13T00:00:00Z",
                        "ended": "2026-08-13T01:00:00Z",
                        "recorded": "2026-08-13T01:00:01Z",
                        "digest": "c" * 64,
                    },
                )

            bootstrap_sqlite_database(engine)

            columns = {
                column["name"]
                for column in inspect(engine).get_columns(
                    "mcp_rollout_evidence_snapshot"
                )
            }
            self.assertTrue(
                {"attestation_key_id", "attestation_signature"}.issubset(columns)
            )
            with engine.connect() as connection:
                row = connection.execute(
                    text(
                        "SELECT evidence_id, attestation_key_id, attestation_signature "
                        "FROM mcp_rollout_evidence_snapshot"
                    )
                ).one()
            self.assertEqual(tuple(row), ("legacy-prod", None, None))

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
                "maf_master_key_validation",
                "mcp_branch_record",
                "mcp_call_record",
                "mcp_remote_task_binding",
                "mcp_remote_task_outbox",
                "mcp_sealed_state",
                "mcp_connection_lease",
                "mcp_audit_event",
                "mcp_legacy_migration_record",
            }.issubset(table_names)
        )
        self.assertNotIn("mcp_credential_key_validation", table_names)

    def test_master_key_validation_table_has_exact_shape_and_constraints(self) -> None:
        inspector = inspect(self.engine)
        self.assertEqual(
            {column["name"] for column in inspector.get_columns("maf_master_key_validation")},
            {
                "singleton_key",
                "validation_nonce",
                "validation_ciphertext",
                "derivation_version",
                "created_at",
            },
        )
        invalid_rows = (
            {
                "singleton_key": 2,
                "validation_nonce": b"n" * 12,
                "derivation_version": 1,
                "created_at": "2026-08-14T00:00:00+00:00",
            },
            {
                "singleton_key": 1,
                "validation_nonce": b"short",
                "derivation_version": 1,
                "created_at": "2026-08-14T00:00:00+00:00",
            },
            {
                "singleton_key": 1,
                "validation_nonce": b"n" * 12,
                "derivation_version": 2,
                "created_at": "2026-08-14T00:00:00+00:00",
            },
            {
                "singleton_key": 1,
                "validation_nonce": b"n" * 12,
                "derivation_version": 1,
                "created_at": None,
            },
        )
        for index, values in enumerate(invalid_rows):
            with self.subTest(values=values), self.assertRaises(IntegrityError):
                with self.engine.begin() as connection:
                    connection.execute(
                        text(
                            "INSERT INTO maf_master_key_validation "
                            "(singleton_key, validation_nonce, validation_ciphertext, "
                            "derivation_version, created_at) "
                            "VALUES (:singleton_key, :validation_nonce, :ciphertext, "
                            ":derivation_version, :created_at)"
                        ),
                        {
                            **values,
                            "ciphertext": f"cipher-{index}".encode(),
                        },
                    )

    def test_bootstrap_leaves_legacy_key_validation_table_unreachable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            engine = create_sqlite_engine(Path(tmpdir) / "legacy-key-validation.sqlite3")
            self.addCleanup(engine.dispose)
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "CREATE TABLE mcp_credential_key_validation ("
                        "validation_id TEXT PRIMARY KEY, singleton_key INTEGER NOT NULL UNIQUE, "
                        "validation_nonce BLOB NOT NULL, validation_ciphertext BLOB NOT NULL, "
                        "encryption_version INTEGER NOT NULL, created_at TEXT)"
                    )
                )
                connection.execute(
                    text(
                        "INSERT INTO mcp_credential_key_validation VALUES "
                        "('legacy', 1, X'00', X'01', 1, NULL)"
                    )
                )

            bootstrap_sqlite_database(engine)

            table_names = set(inspect(engine).get_table_names())
            self.assertIn("mcp_credential_key_validation", table_names)
            self.assertIn("maf_master_key_validation", table_names)
            self.assertNotIn("mcp_credential_key_validation", SQLiteBase.metadata.tables)
            with engine.connect() as connection:
                self.assertEqual(
                    connection.execute(
                        text("SELECT validation_id FROM mcp_credential_key_validation")
                    ).scalar_one(),
                    "legacy",
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

    def test_bootstrap_adds_grant_invalidation_columns_without_losing_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            engine = create_sqlite_engine(Path(tmpdir) / "legacy-mcp-grant.sqlite3")
            try:
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            "CREATE TABLE user_mcp_tool_grant ("
                            "grant_id TEXT PRIMARY KEY, owner_user_id TEXT NOT NULL, "
                            "server_id TEXT NOT NULL, tool_name TEXT NOT NULL, "
                            "server_security_version BIGINT NOT NULL, "
                            "input_schema_sha256 TEXT NOT NULL, granted_at TEXT)"
                        )
                    )
                    connection.execute(
                        text(
                            "INSERT INTO user_mcp_tool_grant "
                            "VALUES ('grant-a', 'alice', 'server-a', 'lookup', 1, 'schema-a', NULL)"
                        )
                    )

                bootstrap_sqlite_database(engine)

                columns = {column["name"] for column in inspect(engine).get_columns("user_mcp_tool_grant")}
                self.assertTrue({"invalidated_at", "invalid_reason"}.issubset(columns))
                with engine.connect() as connection:
                    self.assertEqual(
                        connection.execute(text("SELECT COUNT(*) FROM user_mcp_tool_grant")).scalar_one(),
                        1,
                    )
            finally:
                engine.dispose()

    def test_bootstrap_adds_task_mcp_assignment_columns_without_losing_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            engine = create_sqlite_engine(Path(tmpdir) / "legacy-task.sqlite3")
            try:
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            "CREATE TABLE task ("
                            "task_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, "
                            "root_message_id TEXT NOT NULL, status TEXT NOT NULL, "
                            "routing_mode TEXT NOT NULL, requested_capability_id TEXT, "
                            "root_node_id TEXT, summary TEXT, cancel_requested_at TEXT, "
                            "created_at TEXT, updated_at TEXT)"
                        )
                    )
                    connection.execute(
                        text(
                            "INSERT INTO task "
                            "(task_id, conversation_id, root_message_id, status, routing_mode) "
                            "VALUES ('task-history', 'conv-history', 'msg-history', 'completed', 'auto')"
                        )
                    )

                bootstrap_sqlite_database(engine)

                columns = {column["name"] for column in inspect(engine).get_columns("task")}
                self.assertTrue(
                    {
                        "mcp_execution_mode",
                        "mcp_shadow_enabled",
                        "mcp_rollout_config_version",
                        "mcp_route_reason_code",
                        "mcp_rollout_mode",
                    }.issubset(columns)
                )
                session_factory = create_sqlite_session_factory(engine)
                loaded = asyncio.run(SQLiteStorage(session_factory).get_task("task-history"))
                self.assertIsNotNone(loaded)
                assert loaded is not None
                self.assertIsNone(loaded.mcp_execution_mode)
                self.assertIsNone(loaded.mcp_rollout_mode)
            finally:
                engine.dispose()

    def test_bootstrap_adds_remote_task_claim_columns_without_losing_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            engine = create_sqlite_engine(Path(tmpdir) / "legacy-mcp-task.sqlite3")
            try:
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            "CREATE TABLE mcp_remote_task_binding ("
                            "safe_remote_task_ref TEXT PRIMARY KEY, "
                            "owner_user_id TEXT NOT NULL, task_id TEXT NOT NULL, "
                            "node_id TEXT NOT NULL, call_ref TEXT NOT NULL, "
                            "server_id TEXT NOT NULL, protocol_version TEXT NOT NULL, "
                            "remote_task_ciphertext BLOB NOT NULL, remote_task_nonce BLOB NOT NULL, "
                            "encryption_version INTEGER NOT NULL, last_status TEXT NOT NULL, "
                            "next_poll_at TEXT, created_at TEXT, updated_at TEXT, terminal_at TEXT)"
                        )
                    )
                    connection.execute(
                        text(
                            "INSERT INTO mcp_remote_task_binding VALUES ("
                            "'remote-a', 'alice', 'task-a', 'node-a', 'call-a', 'server-a', "
                            "'2026-07-28', X'01', X'02', 1, 'working', NULL, NULL, NULL, NULL)"
                        )
                    )

                bootstrap_sqlite_database(engine)

                columns = {
                    column["name"]
                    for column in inspect(engine).get_columns("mcp_remote_task_binding")
                }
                self.assertTrue(
                    {"claim_owner", "claim_token", "lease_expires_at", "revision"}.issubset(
                        columns
                    )
                )
                with engine.connect() as connection:
                    row = connection.execute(
                        text(
                            "SELECT safe_remote_task_ref, revision "
                            "FROM mcp_remote_task_binding"
                        )
                    ).one()
                self.assertEqual(row, ("remote-a", 0))
            finally:
                engine.dispose()

    def test_bootstrap_adds_rollout_red_line_identity_without_losing_buckets(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            engine = create_sqlite_engine(Path(tmpdir) / "legacy-rollout-metric.sqlite3")
            try:
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            "CREATE TABLE mcp_rollout_metric_bucket ("
                            "metric_bucket_id TEXT PRIMARY KEY, environment_id TEXT NOT NULL, "
                            "rollout_program TEXT NOT NULL, deployment_id TEXT NOT NULL, "
                            "stage TEXT NOT NULL, config_fingerprint TEXT NOT NULL, "
                            "metric_name TEXT NOT NULL, bucket_started_at TEXT NOT NULL, "
                            "bucket_ended_at TEXT NOT NULL, execution_path TEXT NOT NULL, "
                            "routing_mode TEXT NOT NULL, transport TEXT NOT NULL, "
                            "protocol_version TEXT NOT NULL, adapter TEXT NOT NULL, "
                            "result_category TEXT NOT NULL, error_category TEXT NOT NULL, "
                            "call_kind TEXT NOT NULL, latency_bucket TEXT NOT NULL, "
                            "value BIGINT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
                            "CONSTRAINT uq_mcp_rollout_metric_series_bucket UNIQUE ("
                            "environment_id, deployment_id, stage, config_fingerprint, metric_name, "
                            "bucket_started_at, bucket_ended_at, execution_path, routing_mode, transport, "
                            "protocol_version, adapter, result_category, error_category, call_kind, "
                            "latency_bucket))"
                        )
                    )
                    connection.execute(
                        text(
                            "CREATE INDEX idx_mcp_rollout_metric_window "
                            "ON mcp_rollout_metric_bucket "
                            "(environment_id, deployment_id, stage, bucket_started_at)"
                        )
                    )
                    connection.execute(
                        text(
                            "INSERT INTO mcp_rollout_metric_bucket VALUES ("
                            "'legacy-bucket', 'staging', 'user_mcp_phase3', 'deploy-a', "
                            "'internal_shadow', 'fingerprint-a', 'mcp_route_requests_total', "
                            "'2026-08-13T08:00:00+00:00', '2026-08-13T08:01:00+00:00', "
                            "'legacy', 'shadow', 'streamable_http', '2026-07-28', "
                            "'python_2026', 'succeeded', 'none', 'not_applicable', "
                            "'not_applicable', 2, '2026-08-13T08:00:00+00:00', "
                            "'2026-08-13T08:00:00+00:00')"
                        )
                    )

                bootstrap_sqlite_database(engine)

                columns = {
                    column["name"]
                    for column in inspect(engine).get_columns(
                        "mcp_rollout_metric_bucket"
                    )
                }
                self.assertIn("red_line", columns)
                unique_constraints = inspect(engine).get_unique_constraints(
                    "mcp_rollout_metric_bucket"
                )
                identity = next(
                    constraint
                    for constraint in unique_constraints
                    if constraint["name"] == "uq_mcp_rollout_metric_series_bucket"
                )
                self.assertIn("red_line", identity["column_names"])
                metric_checks = " ".join(
                    str(item.get("sqltext") or "")
                    for item in inspect(engine).get_check_constraints(
                        "mcp_rollout_metric_bucket"
                    )
                )
                self.assertIn("mcp_result_parser_outcomes_total", metric_checks)
                self.assertIn(
                    "mcp_result_parser_duration_seconds", metric_checks
                )
                with engine.connect() as connection:
                    preserved = connection.execute(
                        text(
                            "SELECT metric_bucket_id, red_line, value "
                            "FROM mcp_rollout_metric_bucket"
                        )
                    ).one()
                self.assertEqual(preserved, ("legacy-bucket", "not_applicable", 2))

                now = datetime(2026, 8, 13, 8, 0, tzinfo=timezone.utc)
                storage = SQLiteStorage(create_sqlite_session_factory(engine))
                red_line_bucket = MCPRolloutMetricBucket(
                    metric_bucket_id="red-line-a",
                    environment_id="staging",
                    deployment_id="deploy-a",
                    stage="internal_shadow",
                    config_fingerprint="fingerprint-a",
                    metric_name="mcp_safety_red_line_total",
                    bucket_started_at=now,
                    bucket_ended_at=now + timedelta(minutes=1),
                    execution_path="legacy",
                    routing_mode="shadow",
                    transport="streamable_http",
                    protocol_version="2026-07-28",
                    adapter="python_2026",
                    result_category="succeeded",
                    error_category="none",
                    latency_bucket="not_applicable",
                    value=0,
                    red_line="cross_user_access",
                    created_at=now,
                    updated_at=now,
                )
                asyncio.run(storage.upsert_mcp_rollout_metric_bucket(red_line_bucket))
                asyncio.run(
                    storage.upsert_mcp_rollout_metric_bucket(
                        replace(
                            red_line_bucket,
                            metric_bucket_id="red-line-b",
                            red_line="secret_exposure",
                        )
                    )
                )
                listed = asyncio.run(
                    storage.list_mcp_rollout_metric_buckets(
                        "staging",
                        "deploy-a",
                        "internal_shadow",
                        window_started_at=now,
                        window_ended_at=now + timedelta(minutes=1),
                    )
                )
                self.assertEqual(
                    {item.red_line for item in listed},
                    {None, "cross_user_access", "secret_exposure"},
                )
            finally:
                engine.dispose()
