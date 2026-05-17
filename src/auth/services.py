from __future__ import annotations

import hashlib
import html
import re
import secrets
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta

from src.core.contracts import StoragePort
from src.core.models import AuthSession, AuthUser, CaptchaChallenge
from src.integrations.rust_safety_contract import hmac_sha256_hex, verify_auth_token

NowFn = Callable[[], datetime]
CodeGenerator = Callable[[], str]

USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{3,64}$")


class AuthValidationError(ValueError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class DuplicateUsernameError(ValueError):
    pass


def normalize_username(username: str) -> str:
    return username.strip()


def validate_username(username: str) -> str:
    normalized = normalize_username(username)
    if not USERNAME_PATTERN.fullmatch(normalized):
        raise AuthValidationError(
            "Username must be 3-64 characters and contain only letters, numbers, underscore, hyphen, or dot.",
            code="invalid_username",
        )
    return normalized


def validate_password_policy(password: str) -> None:
    if len(password) < 8:
        raise AuthValidationError("Password must be at least 8 characters.", code="password_too_short")
    if re.search(r"[A-Za-z]", password) is None or re.search(r"\d", password) is None:
        raise AuthValidationError(
            "Password must contain both letters and numbers.",
            code="password_requires_letters_numbers",
        )


class PasswordHasher:
    scheme = "pbkdf2_sha256"
    iterations = 200_000

    def hash_password(self, password: str, *, salt: str | None = None) -> tuple[str, str, str]:
        resolved_salt = salt or secrets.token_hex(16)
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            resolved_salt.encode("utf-8"),
            self.iterations,
        ).hex()
        return digest, resolved_salt, self.scheme

    def verify_password(self, password: str, user: AuthUser) -> bool:
        if user.password_scheme != self.scheme:
            return False
        expected, _, _ = self.hash_password(password, salt=user.password_salt)
        return verify_auth_token(expected, user.password_hash)


class CaptchaService:
    def __init__(
        self,
        storage: StoragePort,
        *,
        now_fn: NowFn,
        code_generator: CodeGenerator | None = None,
        secret: str | None = None,
        ttl_seconds: int = 300,
        max_attempts: int = 5,
    ) -> None:
        self._storage = storage
        self._now_fn = now_fn
        self._code_generator = code_generator or self._random_code
        self._secret = (secret or secrets.token_urlsafe(32)).encode("utf-8")
        self._ttl = timedelta(seconds=ttl_seconds)
        self._max_attempts = max_attempts

    async def create_challenge(self) -> tuple[CaptchaChallenge, str, str]:
        code = self._normalize_code(self._code_generator())
        captcha_id = f"cap-{secrets.token_urlsafe(18)}"
        now = self._now_fn()
        challenge = CaptchaChallenge(
            captcha_id=captcha_id,
            code_hash=self._hash_code(captcha_id, code),
            expires_at=now + self._ttl,
            attempt_count=0,
            created_at=now,
        )
        saved = await self._storage.save_captcha_challenge(challenge)
        return saved, code, self.render_svg(code)

    async def verify(self, captcha_id: str, code: str) -> bool:
        challenge = await self._storage.get_captcha_challenge(captcha_id)
        now = self._now_fn()
        if challenge is None:
            return False
        if challenge.consumed_at is not None or challenge.expires_at <= now or challenge.attempt_count >= self._max_attempts:
            return False

        normalized = self._normalize_code(code)
        next_attempt_count = challenge.attempt_count + 1
        matches = verify_auth_token(challenge.code_hash, self._hash_code(captcha_id, normalized))
        updated = replace(
            challenge,
            attempt_count=next_attempt_count,
            consumed_at=now if matches else None,
        )
        await self._storage.save_captcha_challenge(updated)
        return matches

    def _hash_code(self, captcha_id: str, code: str) -> str:
        payload = f"{captcha_id}:{code}".encode("utf-8")
        return hmac_sha256_hex(self._secret, payload)

    @staticmethod
    def _random_code() -> str:
        return f"{secrets.randbelow(10_000):04d}"

    @staticmethod
    def _normalize_code(code: str) -> str:
        digits = "".join(ch for ch in str(code) if ch.isdigit())
        if len(digits) != 4:
            return ""
        return digits

    @staticmethod
    def render_svg(code: str) -> str:
        safe_code = html.escape(code)
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" width="96" height="36" role="img" '
            'aria-label="4 digit verification code">'
            '<rect width="96" height="36" rx="6" fill="#f5f7fb"/>'
            '<path d="M6 28 C28 6 62 42 90 10" stroke="#b6c2d9" stroke-width="2" fill="none"/>'
            f'<text x="48" y="24" text-anchor="middle" font-family="monospace" font-size="22" '
            f'font-weight="700" fill="#1f2a44" letter-spacing="4">{safe_code}</text>'
            '</svg>'
        )


class SessionService:
    def __init__(
        self,
        storage: StoragePort,
        *,
        now_fn: NowFn,
        ttl_seconds: int = 28_800,
    ) -> None:
        self._storage = storage
        self._now_fn = now_fn
        self._ttl = timedelta(seconds=ttl_seconds)

    async def create_session(self, username: str) -> AuthSession:
        now = self._now_fn()
        session = AuthSession(
            session_id=f"sess-{secrets.token_urlsafe(32)}",
            username=username,
            expires_at=now + self._ttl,
            created_at=now,
        )
        return await self._storage.save_auth_session(session)

    async def get_active_user(self, session_id: str) -> AuthUser | None:
        session = await self._storage.get_auth_session(session_id)
        now = self._now_fn()
        if session is None or session.revoked_at is not None or session.expires_at <= now:
            return None
        user = await self._storage.get_auth_user(session.username)
        if user is None or user.status != "active":
            return None
        return user

    async def revoke_session(self, session_id: str) -> None:
        session = await self._storage.get_auth_session(session_id)
        if session is None or session.revoked_at is not None:
            return
        await self._storage.save_auth_session(replace(session, revoked_at=self._now_fn()))
