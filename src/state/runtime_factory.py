from __future__ import annotations

import importlib.util
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Mapping

from .errors import redact_text


class StatePlatformBackend(StrEnum):
    SQLITE_LEGACY = "sqlite"
    POSTGRESQL = "postgresql"


class StatePlatformConfigError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class StatePlatformRuntimeConfig:
    backend: StatePlatformBackend
    release_gate_configured: bool
    dsn: str | None = field(default=None, repr=False)
    reason: str | None = None

    def public_dict(self) -> dict[str, object]:
        return {
            "backend": self.backend.value,
            "release_gate_configured": self.release_gate_configured,
            "dsn": "<configured>" if self.dsn else None,
            "reason": redact_text(self.reason),
        }


def _deployment_env(env: Mapping[str, str]) -> str:
    return (env.get("MAF_API_ENV") or env.get("MAF_ENV") or env.get("APP_ENV") or "dev").strip().lower()


def _is_production(env: Mapping[str, str]) -> bool:
    return _deployment_env(env) in {"prod", "production"}


def build_state_platform_runtime_config(
    *,
    env: Mapping[str, str],
    require_driver: bool = True,
    driver_available: bool | None = None,
) -> StatePlatformRuntimeConfig:
    backend = (env.get("MAF_STATE_STORE_BACKEND") or "sqlite").strip().lower()
    production = _is_production(env)
    if backend in {"", "sqlite", "sqlite_legacy"}:
        if production:
            raise StatePlatformConfigError("production mode does not allow sqlite canonical state backend")
        return StatePlatformRuntimeConfig(
            backend=StatePlatformBackend.SQLITE_LEGACY,
            release_gate_configured=False,
            reason="dev_test_sqlite_legacy",
        )
    if backend != "postgresql":
        raise StatePlatformConfigError(f"Unsupported state store backend: {backend}")
    dsn = (env.get("MAF_POSTGRES_STATE_DSN") or "").strip()
    if not dsn:
        raise StatePlatformConfigError("PostgreSQL State Platform requires MAF_POSTGRES_STATE_DSN")
    if driver_available is None:
        driver_available = importlib.util.find_spec("psycopg") is not None
    if require_driver and not driver_available:
        raise StatePlatformConfigError("PostgreSQL driver psycopg is not installed")
    if (env.get("MAF_RUNTIME_SIDECAR_WRITER_MODE") or "").strip().lower() == "enforce":
        raise StatePlatformConfigError("State Platform canonical writer conflict with RuntimeSidecar enforce writer")
    return StatePlatformRuntimeConfig(
        backend=StatePlatformBackend.POSTGRESQL,
        release_gate_configured=True,
        dsn=dsn,
        reason="postgresql_state_platform_configured",
    )
