from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from . import (
    decoder_2024_11_05,
    decoder_2025_03_26,
    decoder_2025_06_18,
    decoder_2025_11_25,
    decoder_2026_07_28,
)
from .errors import MCPResultParseError
from .json_values import loads_strict_json, strict_json_value
from .models import MCPParsedToolResult, MCPResultDecodeRequest


Decoder = Callable[[MCPResultDecodeRequest, Mapping[str, Any]], MCPParsedToolResult]

DECODERS: Mapping[str, Decoder] = {
    "2024-11-05": decoder_2024_11_05.decode,
    "2025-03-26": decoder_2025_03_26.decode,
    "2025-06-18": decoder_2025_06_18.decode,
    "2025-11-25": decoder_2025_11_25.decode,
    "2026-07-28": decoder_2026_07_28.decode,
}


def decode_result(request: MCPResultDecodeRequest) -> MCPParsedToolResult:
    decoder = DECODERS.get(request.protocol_version)
    if decoder is None:
        raise MCPResultParseError("unsupported_protocol_version")
    if isinstance(request.payload, (bytes, str)):
        payload = loads_strict_json(request.payload)
    else:
        payload = strict_json_value(request.payload)
    if not isinstance(payload, Mapping):
        raise MCPResultParseError("result_shape_invalid")
    return decoder(request, payload)


__all__ = ["DECODERS", "decode_result"]
