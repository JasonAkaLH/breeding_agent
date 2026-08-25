from __future__ import annotations

import re
import secrets
from collections.abc import Callable
from datetime import datetime

from src.auth.generation_cache import AuthGenerationCache
from src.auth.invalidation_bus import AuthGenerationChanged, AuthGenerationReason, InMemoryAuthInvalidationBus
from src.core.contracts import AuthStoragePort
from src.core.models import AuthUserToken
from src.integrations.master_key import MasterKeyDomain, MasterKeyError, _DerivedDomainKey
from src.integrations.rust_safety_contract import hmac_sha256_hex

NowFn = Callable[[], datetime]
CodeGenerator = Callable[[], str]

USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{3,64}$")


class AuthTokenHasher:
    """Hashes only login tokens with the auth-token domain key."""

    __slots__ = ("_key",)

    def __init__(self, key: _DerivedDomainKey) -> None:
        if not isinstance(key, _DerivedDomainKey):
            raise MasterKeyError("maf_key_domain_invalid")
        self._key = key._consume_for(MasterKeyDomain.AUTH_TOKEN)

    def hash_token(self, raw_token: str) -> str:
        return hmac_sha256_hex(self._key, raw_token.encode("utf-8"))

    def verify_token(self, raw_token: str, expected_hash: str) -> bool:
        return secrets.compare_digest(
            self.hash_token(raw_token),
            str(expected_hash),
        )

    def __reduce__(self) -> object:
        raise TypeError("auth token hashers cannot be serialized")

    def __reduce_ex__(self, _protocol: int) -> object:
        raise TypeError("auth token hashers cannot be serialized")


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
        storage: AuthStoragePort,
        *,
        now_fn: NowFn,
        token_hasher: AuthTokenHasher,
        auth_generation_cache: AuthGenerationCache | None = None,
        auth_invalidation_bus: InMemoryAuthInvalidationBus | None = None,
    ) -> None:
        if not isinstance(token_hasher, AuthTokenHasher):
            raise MasterKeyError("maf_key_domain_invalid")
        self._storage = storage
        self._now_fn = now_fn
        self._token_hasher = token_hasher
        self._auth_generation_cache = auth_generation_cache
        self._auth_invalidation_bus = auth_invalidation_bus

    async def login_username(self, username: str) -> tuple[AuthUserToken, str]:
        normalized_username = validate_username(username)
        raw_token = self._new_raw_token()
        now = self._now_fn()
        token = AuthUserToken(
            username=normalized_username,
            api_token_hash=self._hash_token(raw_token),
            token_issued_at=now,
            token_last_used_at=None,
            created_at=now,
            updated_at=now,
        )
        saved = await self._storage.save_auth_user_token(token, auth_generation_reason="login")
        await self._publish_auth_generation_change(saved, "login")
        return saved, raw_token

    async def get_current_token(self, raw_token: str, *, touch: bool = True) -> AuthUserToken:
        normalized = self._normalize_raw_token(raw_token)
        api_token_hash = self._hash_token(normalized)
        token = await self._storage.get_auth_user_token_by_hash(api_token_hash)
        if token is None or token.api_token_hash != api_token_hash:
            raise AuthTokenValidationError("Invalid API token.", code="authentication_expired")
        if not touch:
            return token
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
            auth_generation_reason="logout",
        )
        if cleared is None:
            raise AuthTokenValidationError("Invalid API token.", code="authentication_expired")
        await self._publish_auth_generation_change(cleared, "logout")
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
            auth_generation_reason="refresh",
        )
        if rotated is None:
            raise AuthTokenValidationError("Invalid API token.", code="authentication_expired")
        await self._publish_auth_generation_change(rotated, "refresh")
        return rotated, new_raw_token

    async def token_is_current_for_username(self, raw_token: str, username: str, *, touch: bool = True) -> bool:
        try:
            current = await self.get_current_token(raw_token, touch=touch)
        except AuthTokenValidationError:
            return False
        return current.username == username

    async def reconcile_auth_generations(self) -> None:
        if self._auth_generation_cache is None:
            return
        tokens = await self._storage.list_auth_user_generations()
        self._auth_generation_cache.reconcile({token.username: token.auth_generation for token in tokens})

    async def _publish_auth_generation_change(self, token: AuthUserToken, reason: AuthGenerationReason) -> None:
        if self._auth_generation_cache is not None:
            self._auth_generation_cache.apply(
                token.username,
                token.auth_generation,
                updated_at=token.auth_generation_updated_at or token.updated_at,
            )
        if self._auth_invalidation_bus is not None:
            await self._auth_invalidation_bus.publish(
                AuthGenerationChanged(
                    username=token.username,
                    auth_generation=token.auth_generation,
                    changed_at=token.auth_generation_updated_at or token.updated_at or self._now_fn(),
                    reason=reason,
                )
            )

    def fingerprint(self, raw_token: str) -> str:
        return self._hash_token(self._normalize_raw_token(raw_token))[:12]

    def _hash_token(self, raw_token: str) -> str:
        return self._token_hasher.hash_token(raw_token)

    @staticmethod
    def _new_raw_token() -> str:
        return f"maf_tok_{secrets.token_urlsafe(32)}"

    @staticmethod
    def _normalize_raw_token(raw_token: str) -> str:
        normalized = str(raw_token or "").strip()
        if not normalized or any(ch.isspace() for ch in normalized) or not normalized.startswith("maf_tok_"):
            raise AuthTokenValidationError("Invalid API token.", code="authentication_expired")
        return normalized
