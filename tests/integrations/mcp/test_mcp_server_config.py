from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.integrations.mcp.config import MCPRuntimeConfig, MCPServerConfig, load_mcp_server_config


class MCPServerConfigFileTests(unittest.TestCase):
    def _write_config(self, directory: Path, payload: dict) -> Path:
        path = directory / "mcp_server_config.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_loads_external_mcp_servers_shape_and_normalizes_headers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_config(
                Path(tmp),
                {
                    "mcpServers": {
                        "seed-server": {
                            "url": "http://example.internal/gateway/mcp/sse",
                            "transport": "legacy_http_sse",
                            "protocolVersion": "2024-11-05",
                            "headers": {"X-Example-Tenant": "tenant-test", "X-Example-User": "user-test"},
                            "enabled": True,
                        }
                    }
                },
            )

            config = load_mcp_server_config(path=path)

        self.assertTrue(config.enabled)
        self.assertEqual(len(config.servers), 1)
        server = config.servers[0]
        self.assertEqual(server.server_id, "seed-server")
        self.assertEqual(server.endpoint, "http://example.internal/gateway/mcp/sse")
        self.assertEqual(server.transport, "legacy_http_sse")
        self.assertEqual(server.protocol_version, "2024-11-05")
        self.assertTrue(server.protocol_version_pinned)
        self.assertEqual(server.request_headers, {"X-Example-Tenant": "tenant-test", "X-Example-User": "user-test"})
        self.assertEqual(server.request_header_names, ("X-Example-Tenant", "X-Example-User"))
        self.assertEqual(server.transport_security, "plaintext_http")
        self.assertEqual(server.validation_error(), "")

    def test_default_path_and_env_path_are_supported(self) -> None:
        with tempfile.TemporaryDirectory() as default_tmp, tempfile.TemporaryDirectory() as env_tmp:
            default_path = self._write_config(
                Path(default_tmp),
                {"mcpServers": {"default-server": {"url": "https://default.example.com/mcp"}}},
            )
            env_path = self._write_config(
                Path(env_tmp),
                {"mcpServers": {"env-server": {"url": "https://env.example.com/mcp"}}},
            )

            default_config = load_mcp_server_config(cwd=Path(default_tmp), env={})
            env_config = load_mcp_server_config(cwd=Path(default_tmp), env={"MAF_MCP_SERVER_CONFIG_PATH": str(env_path)})

        self.assertEqual(default_path.name, "mcp_server_config.json")
        self.assertEqual(default_config.servers[0].server_id, "default-server")
        self.assertEqual(env_config.servers[0].server_id, "env-server")

    def test_alias_conflicts_and_duplicate_sources_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_config(
                Path(tmp),
                {
                    "mcpServers": {
                        "crm": {
                            "serverId": "crm",
                            "url": "https://mcp.example.com/rpc",
                            "protocolVersion": "2025-03-26",
                            "protocol_version": "2025-06-18",
                        }
                    }
                },
            )
            with self.assertRaisesRegex(ValueError, "conflicting protocolVersion"):
                load_mcp_server_config(path=path)

            duplicate_path = self._write_config(
                Path(tmp),
                {"mcpServers": {"crm": {"url": "https://mcp.example.com/rpc"}}},
            )
            base = MCPRuntimeConfig.from_mapping(
                {"enabled": True, "servers": [{"server_id": "crm", "endpoint": "https://existing.example.com/rpc"}]}
            )
            with self.assertRaisesRegex(ValueError, "Duplicate MCP server_id"):
                load_mcp_server_config(path=duplicate_path, base_config=base)

    def test_dangerous_headers_and_auth_header_conflicts_fail_validation_without_value_leak(self) -> None:
        dangerous = MCPServerConfig.from_mapping(
            {
                "server_id": "bad",
                "endpoint": "https://mcp.example.com/rpc",
                "headers": {"Host": "evil.example.com", "X-Example-Tenant": "SECRET-TENANT"},
            }
        )
        dangerous_error = dangerous.validation_error()
        self.assertIn("Unsupported MCP request header", dangerous_error)
        self.assertNotIn("SECRET-TENANT", dangerous_error)

        conflict = MCPServerConfig.from_mapping(
            {
                "server_id": "bad2",
                "endpoint": "https://mcp.example.com/rpc",
                "headers": {"X-Api-Key": "STATIC-SECRET"},
                "auth": {"type": "api_key_env", "apiKeyEnv": "MCP_TOKEN", "headerName": "X-Api-Key"},
            }
        )
        conflict_error = conflict.validation_error()
        self.assertIn("conflicts with auth header", conflict_error)
        self.assertNotIn("STATIC-SECRET", conflict_error)

    def test_real_config_is_gitignored_and_example_is_sanitized(self) -> None:
        gitignore = Path(".gitignore").read_text(encoding="utf-8")
        example = Path("mcp_server_config.example.json")

        self.assertIn("mcp_server_config.json", gitignore)
        self.assertTrue(example.exists())
        example_text = example.read_text(encoding="utf-8")
        self.assertIn("mcpServers", example_text)
        self.assertNotIn("SECRET", example_text.upper())
        self.assertNotIn("118", example_text)
        self.assertNotIn("363", example_text)


if __name__ == "__main__":
    unittest.main()
