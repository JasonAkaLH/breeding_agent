from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .decoder_support import decode_legacy_result
from .models import MCPParsedToolResult, MCPResultDecodeRequest, MCPResultSource


def decode(request: MCPResultDecodeRequest, payload: Mapping[str, Any]) -> MCPParsedToolResult:
    return decode_legacy_result(
        request,
        payload,
        allowed_sources=frozenset({MCPResultSource.TOOLS_CALL}),
        allowed_blocks=frozenset({"text", "image", "resource"}),
        allow_annotations=False,
    )
