from __future__ import annotations


def truncate_utf8(value: str, limit: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    return encoded[:limit].decode("utf-8", errors="ignore")


def safe_attachment_basename(value: object) -> str:
    normalized = str(value or "").replace("\\", "/").rsplit("/", 1)[-1]
    normalized = "".join(
        char
        for char in normalized
        if not (ord(char) < 32 or 127 <= ord(char) <= 159)
    ).strip()
    return truncate_utf8(normalized or "attachment", 255)


def safe_attachment_content_type(value: object) -> str:
    normalized = str(value or "").strip()
    if (
        not normalized
        or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in normalized)
        or len(normalized.encode("utf-8")) > 255
    ):
        return "application/octet-stream"
    return normalized
