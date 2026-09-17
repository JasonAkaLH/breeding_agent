from __future__ import annotations

from typing import Any

import httpx
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    InternalServerError,
    PermissionDeniedError,
    RateLimitError,
)

from src.core.errors import ModelUnavailableError


_UNAVAILABLE_STATUS_CODES = frozenset({401, 403, 408, 429})
_UNAVAILABLE_OPENAI_ERRORS = (
    APIConnectionError,
    APITimeoutError,
    AuthenticationError,
    InternalServerError,
    PermissionDeniedError,
    RateLimitError,
)
_UNAVAILABLE_TRANSPORT_ERRORS = (
    ConnectionError,
    TimeoutError,
    httpx.TimeoutException,
    httpx.TransportError,
)


def raise_for_model_unavailable(exc: Exception) -> None:
    """Map explicit Provider availability failures to the shared safe error."""

    if isinstance(exc, ModelUnavailableError):
        raise exc
    if isinstance(exc, _UNAVAILABLE_OPENAI_ERRORS + _UNAVAILABLE_TRANSPORT_ERRORS):
        raise ModelUnavailableError() from exc
    if isinstance(exc, (APIStatusError, httpx.HTTPStatusError)):
        status_code = _status_code(exc)
        if status_code in _UNAVAILABLE_STATUS_CODES or (
            status_code is not None and status_code >= 500
        ):
            raise ModelUnavailableError() from exc


def _status_code(exc: Exception) -> int | None:
    response: Any = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


__all__ = ["raise_for_model_unavailable"]
