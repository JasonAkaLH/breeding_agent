from __future__ import annotations

from datetime import datetime, timedelta

from src.core.models import AuthSession, AuthUser, CaptchaChallenge, Conversation
from src.storage.sqlite.repositories import SQLiteStateRepository
from tests.storage.support import SQLiteStorageTestCase


class SQLiteAuthRepositoryTest(SQLiteStorageTestCase):
    def test_auth_user_captcha_and_session_round_trip(self) -> None:
        now = datetime(2026, 5, 4, 10, 0, 0)
        user = AuthUser(
            username="alice",
            password_hash="hash-value",
            password_salt="salt-value",
            password_scheme="pbkdf2_sha256",
            status="active",
            created_at=now,
            updated_at=now,
        )
        captcha = CaptchaChallenge(
            captcha_id="cap-1",
            code_hash="code-hash",
            expires_at=now + timedelta(minutes=5),
            attempt_count=0,
            created_at=now,
        )
        session = AuthSession(
            session_id="sess-1",
            username="alice",
            expires_at=now + timedelta(hours=8),
            created_at=now,
        )

        with self.session_factory() as db_session:
            repo = SQLiteStateRepository(db_session)
            repo.save_auth_user(user)
            repo.save_captcha_challenge(captcha)
            repo.save_auth_session(session)
            db_session.commit()

        with self.session_factory() as db_session:
            repo = SQLiteStateRepository(db_session)
            self.assertEqual(repo.get_auth_user("alice"), user)
            self.assertEqual(repo.get_captcha_challenge("cap-1"), captcha)
            self.assertEqual(repo.get_auth_session("sess-1"), session)

    def test_lists_conversations_for_account_only(self) -> None:
        with self.session_factory() as db_session:
            repo = SQLiteStateRepository(db_session)
            repo.save_conversation(Conversation(conversation_id="conv-a1", account_id="alice", title="Alice 1"))
            repo.save_conversation(Conversation(conversation_id="conv-b1", account_id="bob", title="Bob 1"))
            repo.save_conversation(Conversation(conversation_id="conv-a2", account_id="alice", title="Alice 2"))
            db_session.commit()

        with self.session_factory() as db_session:
            repo = SQLiteStateRepository(db_session)
            listed = repo.list_conversations_for_account("alice")

        self.assertEqual([conversation.conversation_id for conversation in listed], ["conv-a2", "conv-a1"])
        self.assertTrue(all(conversation.account_id == "alice" for conversation in listed))
