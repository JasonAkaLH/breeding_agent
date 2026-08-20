from __future__ import annotations

import base64
import binascii
import hashlib
import math
import re
from collections.abc import Mapping
from typing import Any

from .errors import MCPResultParseError
from .models import (
    MCPAudioResultBlock,
    MCPEmbeddedBlobResourceBlock,
    MCPEmbeddedTextResourceBlock,
    MCPImageResultBlock,
    MCPResourceLinkResultBlock,
    MCPResultBlock,
    MCPTextResultBlock,
)


MAX_CONTENT_BLOCKS = 1_024
_MIME_RE = re.compile(r"^[!#$&^_.+\-A-Za-z0-9]+/[!#$&^_.+\-A-Za-z0-9]+$")
_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]{0,31}$")


def decode_content_blocks(
    value: object,
    *,
    allowed_kinds: frozenset[str],
    allow_annotations: bool,
) -> tuple[MCPResultBlock, ...]:
    if not isinstance(value, list) or len(value) > MAX_CONTENT_BLOCKS:
        raise MCPResultParseError("result_shape_invalid")
    return tuple(
        _decode_block(
            item,
            allowed_kinds=allowed_kinds,
            allow_annotations=allow_annotations,
        )
        for item in value
    )


def _decode_block(
    value: object,
    *,
    allowed_kinds: frozenset[str],
    allow_annotations: bool,
) -> MCPResultBlock:
    if not isinstance(value, Mapping) or not isinstance(value.get("type"), str):
        raise MCPResultParseError("content_block_invalid")
    kind = value["type"]
    if kind not in allowed_kinds:
        raise MCPResultParseError("content_block_invalid")
    audience, priority = _annotations(value.get("annotations"), allow_annotations)
    if kind == "text":
        text = value.get("text")
        if not isinstance(text, str):
            raise MCPResultParseError("content_block_invalid")
        return MCPTextResultBlock(text=text, audience=audience, priority=priority)
    if kind in {"image", "audio"}:
        mime_type = _mime(value.get("mimeType"), required=True)
        data = _binary(value.get("data"))
        block_type = MCPImageResultBlock if kind == "image" else MCPAudioResultBlock
        return block_type(
            mime_type=mime_type,
            byte_size=len(data),
            sha256="sha256:" + hashlib.sha256(data).hexdigest(),
            audience=audience,
            priority=priority,
        )
    if kind == "resource":
        resource = value.get("resource")
        if not isinstance(resource, Mapping):
            raise MCPResultParseError("content_block_invalid")
        scheme = _uri_scheme(resource.get("uri"))
        has_text = "text" in resource
        has_blob = "blob" in resource
        if has_text == has_blob:
            raise MCPResultParseError("content_block_invalid")
        if has_text:
            text = resource.get("text")
            if not isinstance(text, str):
                raise MCPResultParseError("content_block_invalid")
            return MCPEmbeddedTextResourceBlock(
                uri_scheme=scheme,
                text=text,
                mime_type=_optional_mime(resource.get("mimeType")),
            )
        data = _binary(resource.get("blob"))
        return MCPEmbeddedBlobResourceBlock(
            uri_scheme=scheme,
            mime_type=_optional_mime(resource.get("mimeType"))
            or "application/octet-stream",
            byte_size=len(data),
            sha256="sha256:" + hashlib.sha256(data).hexdigest(),
        )
    if kind == "resource_link":
        name = value.get("name")
        if not isinstance(name, str):
            raise MCPResultParseError("content_block_invalid")
        return MCPResourceLinkResultBlock(
            name=name,
            uri_scheme=_uri_scheme(value.get("uri")),
            title=_optional_string(value.get("title")),
            description=_optional_string(value.get("description")),
            mime_type=_optional_mime(value.get("mimeType")),
        )
    raise MCPResultParseError("content_block_invalid")


def _annotations(value: object, allowed: bool) -> tuple[tuple[str, ...], float | None]:
    if value is None or not allowed:
        return (), None
    if not isinstance(value, Mapping):
        raise MCPResultParseError("content_block_invalid")
    audience_value = value.get("audience")
    audience: tuple[str, ...] = ()
    if audience_value is not None:
        if (
            not isinstance(audience_value, list)
            or any(item not in {"user", "assistant"} for item in audience_value)
        ):
            raise MCPResultParseError("content_block_invalid")
        audience = tuple(audience_value)
    priority_value = value.get("priority")
    priority: float | None = None
    if priority_value is not None:
        if (
            isinstance(priority_value, bool)
            or not isinstance(priority_value, (int, float))
            or not math.isfinite(float(priority_value))
            or not 0 <= float(priority_value) <= 1
        ):
            raise MCPResultParseError("content_block_invalid")
        priority = float(priority_value)
    return audience, priority


def _binary(value: object) -> bytes:
    if not isinstance(value, str):
        raise MCPResultParseError("content_block_invalid")
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
        raise MCPResultParseError("content_block_invalid") from exc


def _mime(value: object, *, required: bool) -> str:
    if not isinstance(value, str):
        if required:
            raise MCPResultParseError("content_block_invalid")
        return "application/octet-stream"
    normalized = value.split(";", 1)[0].strip().lower()
    if (
        not normalized
        or len(normalized) > 255
        or not normalized.isascii()
        or _MIME_RE.fullmatch(normalized) is None
    ):
        if required:
            raise MCPResultParseError("content_block_invalid")
        return "application/octet-stream"
    return normalized


def _optional_mime(value: object) -> str | None:
    if value is None:
        return None
    return _mime(value, required=False)


def _uri_scheme(value: object) -> str:
    if not isinstance(value, str) or ":" not in value:
        raise MCPResultParseError("content_block_invalid")
    scheme = value.split(":", 1)[0].lower()
    if _SCHEME_RE.fullmatch(scheme) is None:
        raise MCPResultParseError("content_block_invalid")
    return scheme


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise MCPResultParseError("content_block_invalid")
    return value
