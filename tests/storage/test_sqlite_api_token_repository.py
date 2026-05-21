from __future__ import annotations

from datetime import datetime, timedelta

from src.core.models import AuthApiToken, AuthUser
from src.storage.sqlite.repositories import SQLiteStateRepository
from tests.storage.support import SQLiteStorageTestCase


class SQLiteApiTokenRepositoryTest(SQLiteStorageTestCase):
    def test_api_token_round_trip_lists_for_user_and_does_not_store_raw_token(self) -> None:
        now = datetime(2026, 5, 21, 10, 0, 0)
        token = AuthApiToken(
            token_id="tok-1",
            token_hash="hash-not-raw-token",
            username="alice",
            client_name="partner ui",
            scopes=("conversation:read", "conversation:write"),
            expires_at=now + timedelta(hours=8),
            created_at=now,
        )
        bob_token = AuthApiToken(
            token_id="tok-2",
            token_hash="hash-bob",
            username="bob",
            client_name="bob cli",
            scopes=("conversation:read",),
            expires_at=now + timedelta(hours=8),
            created_at=now,
        )

        with self.session_factory() as db_session:
            repo = SQLiteStateRepository(db_session)
            repo.save_auth_user(AuthUser("alice", "hash", "salt", "scheme"))
            repo.save_auth_api_token(token)
            repo.save_auth_api_token(bob_token)
            db_session.commit()

        with self.session_factory() as db_session:
            repo = SQLiteStateRepository(db_session)
            loaded = repo.get_auth_api_token("tok-1")
            by_hash = repo.get_auth_api_token_by_hash("hash-not-raw-token")
            listed = repo.list_auth_api_tokens_for_user("alice")

        self.assertEqual(loaded, token)
        self.assertEqual(by_hash, token)
        self.assertEqual(listed, [token])
        self.assertNotIn("maf_tok_", repr(loaded))

    def test_token_revoke_and_touch_update_only_their_own_fields(self) -> None:
        now = datetime(2026, 5, 21, 10, 0, 0)
        token = AuthApiToken(
            token_id="tok-race",
            token_hash="hash-race",
            username="alice",
            client_name="partner ui",
            scopes=("conversation:read",),
            expires_at=now + timedelta(hours=8),
            created_at=now,
        )

        with self.session_factory() as db_session:
            repo = SQLiteStateRepository(db_session)
            repo.save_auth_api_token(token)
            revoked = repo.revoke_auth_api_token_for_user("alice", "tok-race", revoked_at=now + timedelta(minutes=1))
            touched = repo.touch_auth_api_token_last_used("tok-race", at=now + timedelta(minutes=2))
            db_session.commit()

        self.assertIsNotNone(revoked)
        self.assertIsNone(touched)
        with self.session_factory() as db_session:
            repo = SQLiteStateRepository(db_session)
            loaded = repo.get_auth_api_token("tok-race")

        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.revoked_at, now + timedelta(minutes=1))
        self.assertIsNone(loaded.last_used_at)
