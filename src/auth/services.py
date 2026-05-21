from __future__ import annotations

import hashlib
import html
import re
import secrets
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta

from src.core.contracts import StoragePort
from src.core.models import AuthApiToken, AuthSession, AuthUser, CaptchaChallenge
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


class AuthTokenValidationError(ValueError):
    def __init__(self, message: str, *, code: str = "invalid_token") -> None:
        super().__init__(message)
        self.code = code


class AuthTokenScopeError(PermissionError):
    def __init__(self, missing_scopes: tuple[str, ...]) -> None:
        super().__init__("API token scope is not sufficient.")
        self.missing_scopes = missing_scopes


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


ALLOWED_API_TOKEN_SCOPES = frozenset(
    {
        "conversation:read",
        "conversation:write",
        "task:control",
        "upload:write",
        "capability:read",
    }
)
DEFAULT_API_TOKEN_TTL_SECONDS = 28_800
MAX_API_TOKEN_TTL_SECONDS = 604_800


class ApiTokenService:
    def __init__(
        self,
        storage: StoragePort,
        *,
        now_fn: NowFn,
        secret: str | None = None,
        require_secret: bool = False,
        default_ttl_seconds: int = DEFAULT_API_TOKEN_TTL_SECONDS,
        max_ttl_seconds: int = MAX_API_TOKEN_TTL_SECONDS,
    ) -> None:
        if require_secret and not secret:
            raise AuthTokenValidationError("API token hash secret is required.", code="token_secret_required")
        self._storage = storage
        self._now_fn = now_fn
        self._secret = (secret or secrets.token_urlsafe(32)).encode("utf-8")
        self._default_ttl_seconds = default_ttl_seconds
        self._max_ttl_seconds = max_ttl_seconds

    @property
    def allowed_scopes(self) -> frozenset[str]:
        return ALLOWED_API_TOKEN_SCOPES

    async def create_token(
        self,
        *,
        username: str,
        client_name: str,
        scopes: tuple[str, ...],
        ttl_seconds: int | None = None,
    ) -> tuple[AuthApiToken, str]:
        normalized_client_name = self._validate_client_name(client_name)
        normalized_scopes = self._validate_scopes(scopes)
        resolved_ttl = self._validate_ttl(ttl_seconds)
        now = self._now_fn()
        raw_token = f"maf_tok_{secrets.token_urlsafe(32)}"
        token = AuthApiToken(
            token_id=f"tok-{secrets.token_urlsafe(18)}",
            token_hash=self._hash_token(raw_token),
            username=username,
            client_name=normalized_client_name,
            scopes=normalized_scopes,
            expires_at=now + timedelta(seconds=resolved_ttl),
            created_at=now,
        )
        return await self._storage.save_auth_api_token(token), raw_token

    async def list_tokens_for_user(self, username: str) -> list[AuthApiToken]:
        return await self._storage.list_auth_api_tokens_for_user(username)

    async def revoke_token(self, *, username: str, token_id: str) -> AuthApiToken | None:
        return await self._storage.revoke_auth_api_token_for_user(
            username,
            token_id,
            revoked_at=self._now_fn(),
        )

    async def get_active_user_for_bearer(
        self,
        raw_token: str,
        *,
        required_scopes: tuple[str, ...] = (),
    ) -> tuple[AuthUser, AuthApiToken]:
        normalized = self._normalize_raw_token(raw_token)
        token = await self._storage.get_auth_api_token_by_hash(self._hash_token(normalized))
        now = self._now_fn()
        if token is None or token.revoked_at is not None or token.expires_at <= now:
            raise AuthTokenValidationError("Invalid API token.")
        user = await self._storage.get_auth_user(token.username)
        if user is None or user.status != "active":
            raise AuthTokenValidationError("Invalid API token.")
        missing = tuple(scope for scope in required_scopes if scope not in token.scopes)
        if missing:
            raise AuthTokenScopeError(missing)
        updated = await self._storage.touch_auth_api_token_last_used(token.token_id, at=now)
        if updated is None:
            raise AuthTokenValidationError("Invalid API token.")
        return user, updated

    def fingerprint(self, raw_token: str) -> str:
        return self._hash_token(self._normalize_raw_token(raw_token))[:12]

    def _hash_token(self, raw_token: str) -> str:
        return hmac_sha256_hex(self._secret, raw_token.encode("utf-8"))

    @staticmethod
    def _validate_client_name(client_name: str) -> str:
        normalized = " ".join(str(client_name).strip().split())
        if not normalized or len(normalized) > 80:
            raise AuthTokenValidationError("client_name must be 1-80 characters.", code="invalid_client_name")
        return normalized

    def _validate_scopes(self, scopes: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(dict.fromkeys(str(scope).strip() for scope in scopes if str(scope).strip()))
        if not normalized:
            raise AuthTokenValidationError("At least one API token scope is required.", code="invalid_scope")
        unknown = tuple(scope for scope in normalized if scope not in self.allowed_scopes)
        if unknown:
            raise AuthTokenValidationError(f"Unknown API token scope: {unknown[0]}", code="invalid_scope")
        return normalized

    def _validate_ttl(self, ttl_seconds: int | None) -> int:
        resolved = self._default_ttl_seconds if ttl_seconds is None else int(ttl_seconds)
        if resolved <= 0 or resolved > self._max_ttl_seconds:
            raise AuthTokenValidationError(
                f"ttl_seconds must be between 1 and {self._max_ttl_seconds}.",
                code="invalid_ttl",
            )
        return resolved

    @staticmethod
    def _normalize_raw_token(raw_token: str) -> str:
        normalized = str(raw_token or "").strip()
        if not normalized or any(ch.isspace() for ch in normalized) or not normalized.startswith("maf_tok_"):
            raise AuthTokenValidationError("Invalid API token.")
        return normalized
