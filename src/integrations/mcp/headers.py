from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass


MAX_STATIC_HEADERS = 20
MAX_HEADER_NAME_CHARS = 128
MAX_HEADER_VALUE_BYTES = 4096
_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_PROTECTED_HEADERS = frozenset(
    {
        "accept",
        "authorization",
        "connection",
        "content-length",
        "content-type",
        "cookie",
        "host",
        "keep-alive",
        "last-event-id",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)


class HeaderPolicyError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class SecretHeaderValues:
    _values: tuple[tuple[str, str], ...]

    def reveal(self) -> dict[str, str]:
        return dict(self._values)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(<redacted>)"


@dataclass(frozen=True, slots=True)
class ValidatedStaticHeaders:
    names: tuple[str, ...]
    credential_values: SecretHeaderValues


def normalize_header_name(name: str) -> str:
    if not isinstance(name, str) or not name or len(name) > MAX_HEADER_NAME_CHARS or not name.isascii():
        raise HeaderPolicyError("mcp_header_name_invalid")
    if not _HEADER_NAME_RE.fullmatch(name):
        raise HeaderPolicyError("mcp_header_name_invalid")
    normalized = name.lower()
    if normalized in _PROTECTED_HEADERS or normalized.startswith("mcp-"):
        raise HeaderPolicyError("mcp_header_protected")
    return normalized


def validate_auth_header_name(name: str) -> str:
    return normalize_header_name(name)


def validate_static_headers(headers: Mapping[str, str]) -> ValidatedStaticHeaders:
    if not isinstance(headers, Mapping) or len(headers) > MAX_STATIC_HEADERS:
        raise HeaderPolicyError("mcp_static_headers_invalid")
    values: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw_name, value in headers.items():
        name = normalize_header_name(raw_name)
        if name in seen:
            raise HeaderPolicyError("mcp_header_name_duplicate")
        if not isinstance(value, str) or not value or len(value.encode("utf-8")) > MAX_HEADER_VALUE_BYTES:
            raise HeaderPolicyError("mcp_header_value_invalid")
        if "\r" in value or "\n" in value or any(ord(char) < 0x20 and char != "\t" for char in value):
            raise HeaderPolicyError("mcp_header_value_invalid")
        seen.add(name)
        values.append((name, value))
    values.sort(key=lambda item: item[0])
    return ValidatedStaticHeaders(
        names=tuple(name for name, _ in values),
        credential_values=SecretHeaderValues(tuple(values)),
    )
