from __future__ import annotations

import unittest

from src.integrations.mcp.argument_validation import (
    MCPToolArgumentValidationError,
    validate_mcp_tool_arguments,
)


class MCPToolArgumentValidationTest(unittest.TestCase):
    def test_default_draft7_accepts_valid_arguments(self) -> None:
        validate_mcp_tool_arguments(
            {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            {"query": "Alice"},
        )

    def test_explicit_2020_12_rejects_invalid_arguments_without_echoing_data(self) -> None:
        schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        }

        with self.assertRaises(MCPToolArgumentValidationError) as raised:
            validate_mcp_tool_arguments(schema, {"secret": "Bearer private-token"})

        self.assertEqual(str(raised.exception), "MCP tool arguments are invalid")
        self.assertNotIn("private-token", str(raised.exception))

    def test_invalid_schema_uses_same_stable_error(self) -> None:
        with self.assertRaises(MCPToolArgumentValidationError) as raised:
            validate_mcp_tool_arguments({"type": "not-a-json-schema-type"}, {})

        self.assertEqual(str(raised.exception), "MCP tool arguments are invalid")


if __name__ == "__main__":
    unittest.main()
