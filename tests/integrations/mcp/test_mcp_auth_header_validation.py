from __future__ import annotations

import unittest

from src.integrations.mcp.config import MCPServerConfig


class MCPAuthHeaderValidationTests(unittest.TestCase):
    def test_api_key_auth_rejects_dangerous_header_names(self) -> None:
        server = MCPServerConfig.from_mapping(
            {
                "server_id": "bad-auth",
                "endpoint": "https://mcp.example.com/rpc",
                "auth": {"type": "api_key_env", "apiKeyEnv": "MCP_TOKEN", "headerName": "Host"},
            }
        )

        self.assertIn("Unsupported MCP auth header", server.validation_error())

    def test_api_key_auth_rejects_invalid_header_names(self) -> None:
        server = MCPServerConfig.from_mapping(
            {
                "server_id": "bad-auth",
                "endpoint": "https://mcp.example.com/rpc",
                "auth": {"type": "api_key_env", "apiKeyEnv": "MCP_TOKEN", "headerName": "Bad Header"},
            }
        )

        self.assertIn("Invalid MCP auth header", server.validation_error())


if __name__ == "__main__":
    unittest.main()
