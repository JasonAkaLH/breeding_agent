from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping

RETRYABLE_SQLSTATES: Mapping[str, str] = {
    "40P01": "postgres_deadlock",
    "40001": "postgres_serialization_failure",
    "55P03": "postgres_lock_not_available",
    "57014": "postgres_query_canceled",
}

_SECRET_PATTERNS = (
    re.compile(r"postgres(?:ql)?://[^\s'\"]+", re.IGNORECASE),
    re.compile(r"(?i)(api[_-]?token|token|password|passwd|secret)=([^\s,;]+)"),
)


def redact_text(value: str | None) -> str | None:
    if value is None:
        return None
    redacted = value
    redacted = _SECRET_PATTERNS[0].sub("<redacted-dsn>", redacted)
    redacted = _SECRET_PATTERNS[1].sub(lambda m: f"{m.group(1)}=<redacted>", redacted)
    return redacted


def _coerce_sqlstate(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip().upper()
    if re.fullmatch(r"[0-9A-Z]{5}", value):
        return value
    return None


def extract_sqlstate(error: BaseException | None) -> str | None:
    """Best-effort SQLSTATE extraction across psycopg/asyncpg/SQLAlchemy wrappers."""
    seen: set[int] = set()
    current: Any = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        for attr in ("sqlstate", "pgcode", "sql_state"):
            sqlstate = _coerce_sqlstate(getattr(current, attr, None))
            if sqlstate:
                return sqlstate
        diag = getattr(current, "diag", None)
        if diag is not None:
            sqlstate = _coerce_sqlstate(getattr(diag, "sqlstate", None))
            if sqlstate:
                return sqlstate
        current = getattr(current, "orig", None) or getattr(current, "__cause__", None) or getattr(current, "__context__", None)
    return None


@dataclass(slots=True, frozen=True)
class StatePlatformError:
    code: str
    message: str = field(repr=False)
    retryable: bool = False
    sqlstate: str | None = None
    operation: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def public_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": redact_text(self.message),
            "retryable": self.retryable,
            "sqlstate": self.sqlstate,
            "operation": self.operation,
            "metadata": _redact_mapping(self.metadata),
        }


def redact_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): ("<redacted>" if any(marker in str(key).lower() for marker in ("dsn", "token", "password", "secret", "api_key", "apikey")) else redact_value(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item) for item in value)
    if isinstance(value, str):
        return redact_text(value)
    return value


def _redact_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    return redact_value(value)


def classify_state_error(error: BaseException, *, operation: str | None = None) -> StatePlatformError:
    sqlstate = extract_sqlstate(error)
    code = RETRYABLE_SQLSTATES.get(sqlstate or "", "state_platform_error")
    return StatePlatformError(
        code=code,
        message=redact_text(str(error)) or code,
        retryable=sqlstate in RETRYABLE_SQLSTATES,
        sqlstate=sqlstate,
        operation=operation,
    )
