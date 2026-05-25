from __future__ import annotations

import asyncio
from dataclasses import fields
from datetime import datetime

from sqlalchemy import inspect

from src.auth import AuthTokenValidationError, UsernameTokenService
import src.core.models as core_models
from src.storage.sqlite import SQLiteStorage
from src.storage.sqlite.repositories import SQLiteStateRepository
from tests.storage.support import SQLiteStorageTestCase


class SQLiteUsernameAuthRepositoryContractTest(SQLiteStorageTestCase):
    def test_username_owner_replaces_account_id_on_public_core_models(self) -> None:
        for model_name in ("Conversation", "ConversationMemorySummary", "PendingSkillContext"):
            names = {field.name for field in fields(getattr(core_models, model_name))}
            self.assertIn("username", names, model_name)
            self.assertNotIn("account_id", names, model_name)

    def test_bootstrap_creates_single_token_mapping_table(self) -> None:
        inspector = inspect(self.engine)
        self.assertIn("auth_user_token", inspector.get_table_names())
        columns = {column["name"] for column in inspector.get_columns("auth_user_token")}
        self.assertGreaterEqual(
            columns,
            {"username", "api_token_hash", "token_issued_at", "token_last_used_at", "created_at", "updated_at"},
        )

    def test_single_token_mapping_round_trip_rotate_clear_and_touch(self) -> None:
        AuthUserToken = getattr(core_models, "AuthUserToken", None)
        self.assertIsNotNone(AuthUserToken, "AuthUserToken core model is required for username-only auth")
        now = datetime(2026, 5, 25, 12, 0, 0)

        with self.session_factory() as session:
            repo = SQLiteStateRepository(session)
            saved = repo.save_auth_user_token(AuthUserToken(username="alice", api_token_hash="hash-1", token_issued_at=now, created_at=now, updated_at=now))
            session.commit()
        self.assertEqual(saved.username, "alice")
        self.assertEqual(saved.api_token_hash, "hash-1")

        with self.session_factory() as session:
            repo = SQLiteStateRepository(session)
            self.assertEqual(repo.get_auth_user_token_by_hash("hash-1").username, "alice")
            stale_rotate = repo.rotate_auth_user_token(
                "alice",
                old_api_token_hash="stale-hash",
                new_api_token_hash="hash-stale",
                at=now,
            )
            self.assertIsNone(stale_rotate)
            rotated = repo.rotate_auth_user_token(
                "alice",
                old_api_token_hash="hash-1",
                new_api_token_hash="hash-2",
                at=now,
            )
            session.commit()
        self.assertEqual(rotated.api_token_hash, "hash-2")

        with self.session_factory() as session:
            repo = SQLiteStateRepository(session)
            self.assertIsNone(repo.get_auth_user_token_by_hash("hash-1"))
            self.assertEqual(repo.get_auth_user_token_by_hash("hash-2").username, "alice")
            touched = repo.touch_auth_user_token_last_used("alice", api_token_hash="hash-2", at=datetime(2026, 5, 25, 12, 1, 0))
            session.commit()
        self.assertEqual(touched.token_last_used_at, datetime(2026, 5, 25, 12, 1, 0))

        with self.session_factory() as session:
            repo = SQLiteStateRepository(session)
            cleared = repo.clear_auth_user_token("alice", api_token_hash="hash-2", at=datetime(2026, 5, 25, 12, 2, 0))
            session.commit()
        self.assertEqual(cleared.username, "alice")
        self.assertIsNone(cleared.api_token_hash)

        with self.session_factory() as session:
            repo = SQLiteStateRepository(session)
            self.assertIsNone(repo.get_auth_user_token_by_hash("hash-2"))
            self.assertEqual(repo.get_auth_user_token("alice").username, "alice")

    def test_username_auth_service_rotates_hash_and_never_persists_plaintext(self) -> None:
        async def scenario() -> None:
            service = UsernameTokenService(
                SQLiteStorage(self.session_factory),
                now_fn=lambda: datetime(2026, 5, 25, 12, 0, 0),
                secret="test-secret",
                require_secret=True,
            )
            token_record, first = await service.login_username("alice")
            self.assertEqual(token_record.username, "alice")
            self.assertTrue(first.startswith("maf_tok_"))

            storage = SQLiteStorage(self.session_factory)
            row = await storage.get_auth_user_token("alice")
            self.assertIsNotNone(row)
            self.assertNotEqual(row.api_token_hash, first)
            self.assertNotIn(first, repr(row))
            self.assertEqual((await service.get_current_token(first)).username, "alice")

            _record, second = await service.refresh_bearer(first)
            self.assertNotEqual(first, second)
            with self.assertRaises(AuthTokenValidationError):
                await service.get_current_token(first)
            self.assertEqual((await service.get_current_token(second)).username, "alice")

            cleared = await service.logout_bearer(second)
            self.assertEqual(cleared.username, "alice")
            self.assertIsNone(cleared.api_token_hash)
            with self.assertRaises(AuthTokenValidationError):
                await service.get_current_token(second)

        asyncio.run(scenario())
