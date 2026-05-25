from __future__ import annotations

import re
import secrets
from collections.abc import Callable
from datetime import datetime

from src.core.contracts import StoragePort
from src.core.models import AuthUserToken
from src.integrations.rust_safety_contract import hmac_sha256_hex

NowFn = Callable[[], datetime]
CodeGenerator = Callable[[], str]

USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{3,64}$")


class AuthValidationError(ValueError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class AuthTokenValidationError(ValueError):
    def __init__(self, message: str, *, code: str = "invalid_token") -> None:
        super().__init__(message)
        self.code = code


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


class UsernameTokenService:
    """Internal username -> one current API token mapping service."""

    def __init__(
        self,
        storage: StoragePort,
        *,
        now_fn: NowFn,
        secret: str | None = None,
        require_secret: bool = False,
    ) -> None:
        if require_secret and not secret:
            raise AuthTokenValidationError("API token hash secret is required.", code="token_secret_required")
        self._storage = storage
        self._now_fn = now_fn
        self._secret = (secret or secrets.token_urlsafe(32)).encode("utf-8")

    async def login_username(self, username: str) -> tuple[AuthUserToken, str]:
        normalized_username = validate_username(username)
        raw_token = self._new_raw_token()
        now = self._now_fn()
        existing = await self._storage.get_auth_user_token(normalized_username)
        token = AuthUserToken(
            username=normalized_username,
            api_token_hash=self._hash_token(raw_token),
            token_issued_at=now,
            token_last_used_at=None,
            created_at=existing.created_at if existing is not None else now,
            updated_at=now,
        )
        return await self._storage.save_auth_user_token(token), raw_token

    async def get_current_token(self, raw_token: str) -> AuthUserToken:
        normalized = self._normalize_raw_token(raw_token)
        api_token_hash = self._hash_token(normalized)
        token = await self._storage.get_auth_user_token_by_hash(api_token_hash)
        if token is None or token.api_token_hash != api_token_hash:
            raise AuthTokenValidationError("Invalid API token.", code="authentication_expired")
        touched = await self._storage.touch_auth_user_token_last_used(
            token.username,
            api_token_hash=api_token_hash,
            at=self._now_fn(),
        )
        if touched is None:
            raise AuthTokenValidationError("Invalid API token.", code="authentication_expired")
        return touched

    async def logout_bearer(self, raw_token: str) -> AuthUserToken:
        current = await self.get_current_token(raw_token)
        assert current.api_token_hash is not None
        cleared = await self._storage.clear_auth_user_token(
            current.username,
            api_token_hash=current.api_token_hash,
            at=self._now_fn(),
        )
        if cleared is None:
            raise AuthTokenValidationError("Invalid API token.", code="authentication_expired")
        return cleared

    async def refresh_bearer(self, raw_token: str) -> tuple[AuthUserToken, str]:
        normalized = self._normalize_raw_token(raw_token)
        old_api_token_hash = self._hash_token(normalized)
        current = await self._storage.get_auth_user_token_by_hash(old_api_token_hash)
        if current is None or current.api_token_hash != old_api_token_hash:
            raise AuthTokenValidationError("Invalid API token.", code="authentication_expired")
        new_raw_token = self._new_raw_token()
        now = self._now_fn()
        rotated = await self._storage.rotate_auth_user_token(
            current.username,
            old_api_token_hash=old_api_token_hash,
            new_api_token_hash=self._hash_token(new_raw_token),
            at=now,
        )
        if rotated is None:
            raise AuthTokenValidationError("Invalid API token.", code="authentication_expired")
        return rotated, new_raw_token

    async def token_is_current_for_username(self, raw_token: str, username: str) -> bool:
        try:
            current = await self.get_current_token(raw_token)
        except AuthTokenValidationError:
            return False
        return current.username == username

    def fingerprint(self, raw_token: str) -> str:
        return self._hash_token(self._normalize_raw_token(raw_token))[:12]

    def _hash_token(self, raw_token: str) -> str:
        return hmac_sha256_hex(self._secret, raw_token.encode("utf-8"))

    @staticmethod
    def _new_raw_token() -> str:
        return f"maf_tok_{secrets.token_urlsafe(32)}"

    @staticmethod
    def _normalize_raw_token(raw_token: str) -> str:
        normalized = str(raw_token or "").strip()
        if not normalized or any(ch.isspace() for ch in normalized) or not normalized.startswith("maf_tok_"):
            raise AuthTokenValidationError("Invalid API token.", code="authentication_expired")
        return normalized
