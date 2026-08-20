from __future__ import annotations


class MCPResultParseError(ValueError):
    """Closed malformed-result failure; wire values never enter the message."""

    _ALLOWED_CODES = frozenset(
        {
            "unsupported_protocol_version",
            "unsupported_result_source",
            "malformed_json",
            "result_shape_invalid",
            "content_block_invalid",
            "output_schema_invalid",
            "output_schema_validation_failed",
        }
    )

    def __init__(self, code: str) -> None:
        if code not in self._ALLOWED_CODES:
            raise ValueError("unknown MCP result parse error code")
        self.code = code
        super().__init__(code)
