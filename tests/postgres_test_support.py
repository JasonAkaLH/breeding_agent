from __future__ import annotations

import os


def isolated_postgres_test_dsn_or_skip_reason(
    primary_env: str,
    *,
    fallback_env: str | None = None,
) -> tuple[str | None, str | None]:
    """Resolve a module-isolated PostgreSQL DSN without breaking legacy gates."""

    env_names = (
        (primary_env,)
        if fallback_env is None
        else (primary_env, fallback_env)
    )
    for env_name in env_names:
        dsn = os.environ.get(env_name, "").strip()
        if dsn:
            return dsn, None
    return None, f"{primary_env.lower()}_not_configured"
