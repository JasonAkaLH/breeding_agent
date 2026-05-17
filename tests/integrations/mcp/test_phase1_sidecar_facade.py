from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from src.integrations.mcp.config import MCPRuntimeConfig
from src.integrations.mcp.protocol import MCP_PROTOCOL_VERSION
from src.integrations.mcp.rust_contract import load_mcp_runtime_contract
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
        self._tmpdir = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmpdir.name)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()
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
        config = MCPRuntimeConfig.from_mapping(
            {
                "enabled": True,
                "rust_runtime": {"mode": "enforce", "endpoint": "unix:///tmp/maf-mcp.sock"},
            }
        )

        self.assertEqual(config.rust_runtime.mode, MCPSidecarMode.ENFORCE)
        self.assertEqual(config.rust_runtime.endpoint, "unix:///tmp/maf-mcp.sock")
        self.assertEqual(MCPRuntimeConfig.disabled().rust_runtime.mode, MCPSidecarMode.OFF)

    def test_enforce_settings_require_allowlisted_artifact_manifest(self) -> None:
        missing = MCPRustRuntimeSettings.from_mapping({"mode": "enforce", "endpoint": "unix:///tmp/maf-mcp.sock"})
        self.assertIn("mcp_runtime_artifact_untrusted", missing.validation_error())

        manifest_path, allowlist_path, _metadata = self._write_mcp_sidecar_artifact_trust_files()
        valid = MCPRustRuntimeSettings.from_mapping(
            {
                "mode": "enforce",
                "endpoint": "unix:///tmp/maf-mcp.sock",
                "artifact_manifest_path": str(manifest_path),
                "artifact_allowlist_path": str(allowlist_path),
            }
        )
        self.assertEqual(valid.validation_error(), "")

    def test_sidecar_client_validates_artifact_provenance_when_supplied(self) -> None:
        _manifest_path, _allowlist_path, metadata = self._write_mcp_sidecar_artifact_trust_files()

        client = MCPSidecarClient(
            transport=InMemoryMCPSidecarTransport(),
            artifact_provenance=metadata,
            allowed_artifact_checksums=("sha256:mcp-sidecar",),
            allowed_cargo_lock_digests=("sha256:cargo-lock",),
        )
        self.assertFalse(client.ready)

        with self.assertRaisesRegex(RuntimeError, "mcp_runtime_artifact_untrusted"):
            MCPSidecarClient(
                transport=InMemoryMCPSidecarTransport(),
                artifact_provenance={**metadata, "schema_hash": "different"},
                allowed_artifact_checksums=("sha256:mcp-sidecar",),
                allowed_cargo_lock_digests=("sha256:cargo-lock",),
            )

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
        manifest_path, allowlist_path, _metadata = self._write_mcp_sidecar_artifact_trust_files()
        config = MCPRuntimeConfig.from_mapping(
            {
                "enabled": True,
                "rust_runtime": {
                    "mode": "enforce",
                    "endpoint": "unix:///tmp/maf-mcp.sock",
                    "artifact_manifest_path": str(manifest_path),
                    "artifact_allowlist_path": str(allowlist_path),
                },
            }
        )
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

    def _write_mcp_sidecar_artifact_trust_files(self) -> tuple[Path, Path, dict[str, str]]:
        contract = load_mcp_runtime_contract()
        manifest_payload = {
            "schema_version": "maf.rust_artifact_provenance.v1",
            "component": "maf_mcp_runtime",
            "artifact_id": "maf_mcp_runtime_sidecar",
            "artifact_kind": "sidecar_binary",
            "artifact_name": "maf-mcp-runtime-sidecar-linux-x86_64",
            "artifact_sha256": "sha256:mcp-sidecar",
            "cargo_lock_sha256": "sha256:cargo-lock",
            "sbom_sha256": "sha256:sbom",
            "provenance_sha256": "sha256:provenance",
            "source": "ci_pipeline",
            "git_commit": "abcdef123456",
            "toolchain": "rustc 1.95.0",
            "target_triple": "x86_64-unknown-linux-gnu",
            "build_profile": "release",
            "cargo_features": ["default"],
            "contract_hashes": {
                "mcp_runtime": contract["schema_hash"],
                "mcp_runtime_errors": contract["error_code_table_hash"],
            },
            "proto_hashes": {"mcp": "maf_mcp_proto_v1_20260517"},
        }
        allowlist_payload = {
            "schema_version": "maf.rust_artifact_allowlist.v1",
            "allowed_artifacts": [manifest_payload],
        }
        manifest_path = self.workspace / "mcp-sidecar.manifest.json"
        allowlist_path = self.workspace / "mcp-sidecar.allowlist.json"
        manifest_path.write_text(json.dumps(manifest_payload), encoding="utf-8")
        allowlist_path.write_text(json.dumps(allowlist_payload), encoding="utf-8")
        metadata = {
            "source": "ci_pipeline",
            "artifact_kind": "mcp_runtime_sidecar_binary",
            "checksum_sha256": "sha256:mcp-sidecar",
            "cargo_lock_digest": "sha256:cargo-lock",
            "protocol_version": contract["protocol_version"],
            "schema_hash": contract["schema_hash"],
            "error_code_table_hash": contract["error_code_table_hash"],
            "proto_hash": "maf_mcp_proto_v1_20260517",
            "sbom_digest": "sha256:sbom",
            "provenance_attestation": "sha256:provenance",
        }
        return manifest_path, allowlist_path, metadata


if __name__ == "__main__":
    unittest.main()
