from __future__ import annotations

import os
import unittest

from src.integrations.mcp.config import MCPRuntimeConfig
from src.integrations.mcp.protocol import MCP_PROTOCOL_VERSION
from src.integrations.mcp.sidecar import (
    MCP_SIDECAR_COMPONENT,
    MCP_SIDECAR_PROTOCOL_VERSION,
    MCPFeatureUnsupportedError,
    MCPSidecarCompatibilityError,
    MCPSidecarMode,
    MCPSidecarVersionInfo,
    MCPRustRuntimeSettings,
    InMemoryMCPSidecarTransport,
    MCPSidecarClient,
)


class MCPPhase1SidecarFacadeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._old_mode = os.environ.pop("MAF_RUST_MCP_RUNTIME_MODE", None)

    def tearDown(self) -> None:
        if self._old_mode is not None:
            os.environ["MAF_RUST_MCP_RUNTIME_MODE"] = self._old_mode
        else:
            os.environ.pop("MAF_RUST_MCP_RUNTIME_MODE", None)

    def test_runtime_mode_defaults_off_and_rejects_invalid_values(self) -> None:
        self.assertEqual(MCPRustRuntimeSettings.from_env().mode, MCPSidecarMode.OFF)

        os.environ["MAF_RUST_MCP_RUNTIME_MODE"] = "shadow"
        self.assertEqual(MCPRustRuntimeSettings.from_env().mode, MCPSidecarMode.SHADOW)

        os.environ["MAF_RUST_MCP_RUNTIME_MODE"] = "invalid"
        with self.assertRaisesRegex(ValueError, "MAF_RUST_MCP_RUNTIME_MODE"):
            MCPRustRuntimeSettings.from_env()

    async def test_sidecar_handshake_separates_internal_protocol_from_external_mcp_version(self) -> None:
        version = MCPSidecarVersionInfo(
            component=MCP_SIDECAR_COMPONENT,
            build_version="test-build",
            protocol_version=MCP_SIDECAR_PROTOCOL_VERSION,
            schema_hash="schema-v1",
            error_code_table_hash="errors-v1",
            supported_features=frozenset({"short_call", "streamable_http"}),
            min_client_version="0.1.0",
            max_client_version="0.1.x",
            external_mcp_protocol_version=MCP_PROTOCOL_VERSION,
        )
        transport = InMemoryMCPSidecarTransport(version=version)
        client = MCPSidecarClient(
            transport=transport,
            expected_schema_hash="schema-v1",
            expected_error_code_table_hash="errors-v1",
        )

        self.assertFalse(client.ready)
        negotiated = await client.handshake()

        self.assertTrue(client.ready)
        self.assertEqual(negotiated.protocol_version, MCP_SIDECAR_PROTOCOL_VERSION)
        self.assertEqual(negotiated.external_mcp_protocol_version, MCP_PROTOCOL_VERSION)
        self.assertNotEqual(negotiated.protocol_version, negotiated.external_mcp_protocol_version)
        self.assertEqual(await client.readiness(), {"ready": True, "component": MCP_SIDECAR_COMPONENT})

    async def test_sidecar_handshake_fails_closed_on_component_schema_or_feature_mismatch(self) -> None:
        bad_version = MCPSidecarVersionInfo(
            component="wrong_component",
            build_version="test-build",
            protocol_version=MCP_SIDECAR_PROTOCOL_VERSION,
            schema_hash="schema-v1",
            error_code_table_hash="errors-v1",
            supported_features=frozenset({"short_call"}),
            min_client_version="0.1.0",
            max_client_version="0.1.x",
            external_mcp_protocol_version=MCP_PROTOCOL_VERSION,
        )
        client = MCPSidecarClient(
            transport=InMemoryMCPSidecarTransport(version=bad_version),
            expected_schema_hash="schema-v1",
            expected_error_code_table_hash="errors-v1",
        )

        with self.assertRaises(MCPSidecarCompatibilityError):
            await client.handshake(required_features={"short_call"})
        self.assertFalse(client.ready)

        good_component_bad_hash = bad_version.with_updates(component=MCP_SIDECAR_COMPONENT, schema_hash="other")
        client = MCPSidecarClient(
            transport=InMemoryMCPSidecarTransport(version=good_component_bad_hash),
            expected_schema_hash="schema-v1",
            expected_error_code_table_hash="errors-v1",
        )
        with self.assertRaisesRegex(MCPSidecarCompatibilityError, "schema_hash"):
            await client.handshake()

    async def test_feature_gate_blocks_undeclared_sidecar_features(self) -> None:
        version = MCPSidecarVersionInfo(
            component=MCP_SIDECAR_COMPONENT,
            build_version="test-build",
            protocol_version=MCP_SIDECAR_PROTOCOL_VERSION,
            schema_hash="schema-v1",
            error_code_table_hash="errors-v1",
            supported_features=frozenset({"short_call"}),
            min_client_version="0.1.0",
            max_client_version="0.1.x",
            external_mcp_protocol_version=MCP_PROTOCOL_VERSION,
        )
        client = MCPSidecarClient(
            transport=InMemoryMCPSidecarTransport(version=version),
            expected_schema_hash="schema-v1",
            expected_error_code_table_hash="errors-v1",
        )
        await client.handshake()

        client.require_feature("short_call")
        with self.assertRaises(MCPFeatureUnsupportedError):
            client.require_feature("mcp_tasks")

    def test_runtime_config_carries_rust_sidecar_settings_without_enabling_by_default(self) -> None:
        config = MCPRuntimeConfig.from_mapping({"enabled": True, "rust_runtime": {"mode": "enforce", "endpoint": "unix:///tmp/maf-mcp.sock"}})

        self.assertEqual(config.rust_runtime.mode, MCPSidecarMode.ENFORCE)
        self.assertEqual(config.rust_runtime.endpoint, "unix:///tmp/maf-mcp.sock")
        self.assertEqual(MCPRuntimeConfig.disabled().rust_runtime.mode, MCPSidecarMode.OFF)

    def test_sidecar_endpoint_allowlist_requires_exact_internal_host_or_unix_socket(self) -> None:
        for endpoint in (
            "unix:///tmp/maf-mcp.sock",
            "http://localhost:38090/rpc",
            "http://127.0.0.1:38090/rpc",
            "http://[::1]:38090/rpc",
        ):
            with self.subTest(endpoint=endpoint):
                settings = MCPRustRuntimeSettings.from_mapping({"mode": "shadow", "endpoint": endpoint})
                self.assertEqual(settings.validation_error(), "")

        for endpoint in (
            "http://localhost.evil.example/rpc",
            "http://127.0.0.1.evil.example/rpc",
            "http://evil@localhost.evil.example/rpc",
            "http://evil@localhost:38090/rpc",
            "http://localhost@127.0.0.1:38090/rpc",
            "http://[::1.evil]/rpc",
            "https://localhost/rpc",
        ):
            with self.subTest(endpoint=endpoint):
                settings = MCPRustRuntimeSettings.from_mapping({"mode": "shadow", "endpoint": endpoint})
                self.assertIn("internal", settings.validation_error())

    async def test_default_sidecar_transport_only_advertises_implemented_phase1_features(self) -> None:
        client = MCPSidecarClient(transport=InMemoryMCPSidecarTransport())

        version = await client.handshake()

        self.assertIn("health", version.supported_features)
        self.assertIn("compatibility_handshake", version.supported_features)
        self.assertNotIn("mcp_tasks", version.supported_features)
        with self.assertRaises(MCPSidecarCompatibilityError):
            await MCPSidecarClient(transport=InMemoryMCPSidecarTransport()).handshake(required_features={"mcp_tasks"})

    async def test_runtime_state_enforce_fails_closed_when_sidecar_is_unavailable(self) -> None:
        config = MCPRuntimeConfig.from_mapping({"enabled": True, "rust_runtime": {"mode": "enforce", "endpoint": "unix:///tmp/maf-mcp.sock"}})
        from src.integrations.mcp.runtime_state import MCPRuntimeState

        state = MCPRuntimeState(config=config, client_factory=lambda _server: None)
        result = await state.refresh(reason="startup", force=True)

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error_type, "MCPSidecarUnavailable")

    async def test_runtime_state_shadow_keeps_python_visible_path_when_sidecar_is_unavailable(self) -> None:
        from src.integrations.mcp.runtime_state import MCPRuntimeState

        class FakeClient:
            async def list_tools(self):
                return [{"name": "search_customer", "inputSchema": {"type": "object"}}]

            async def close(self):
                pass

        config = MCPRuntimeConfig.from_mapping(
            {
                "enabled": True,
                "rust_runtime": {"mode": "shadow", "endpoint": "unix:///tmp/maf-mcp.sock"},
                "servers": [
                    {
                        "server_id": "crm",
                        "endpoint": "https://mcp.example.com/rpc",
                        "tools": [
                            {
                                "tool_name": "search_customer",
                                "expose": True,
                                "capability_id": "mcp.crm.search_customer",
                                "public_name": "Customer Search",
                                "public_description": "查询客户。",
                                "risk_level": "read_only",
                            }
                        ],
                    }
                ],
            }
        )
        state = MCPRuntimeState(config=config, client_factory=lambda _server: FakeClient())
        result = await state.refresh(reason="startup", force=True)

        self.assertEqual(result.status, "completed")
        self.assertEqual([diag.reason for diag in state.active_bundle.diagnostics], ["sidecar_unavailable"])


if __name__ == "__main__":
    unittest.main()
