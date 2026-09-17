from __future__ import annotations

import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

from tests.fixtures.mcp.protocol_fixtures import (
    MCP_PROTOCOL_VERSION,
    MCPFixtureError,
    assert_no_raw_sensitive_values,
    validate_jsonrpc_object_only,
)
from src.integrations.mcp.protocol import (
    SUPPORTED_MCP_PROTOCOL_VERSION_ORDER,
)

FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "fixtures" / "mcp"
MESSAGE_ROOT = FIXTURE_ROOT / "messages"
CONTRACT_ROOT = FIXTURE_ROOT / "contracts"

class MCPPhase0ContractArtifactTests(unittest.TestCase):
    def test_all_message_fixtures_are_single_jsonrpc_objects(self) -> None:
        files = sorted(MESSAGE_ROOT.glob("*.json"))
        self.assertGreaterEqual(len(files), 10)
        for path in files:
            with self.subTest(path=path.name):
                payload = json.loads(path.read_text(encoding="utf-8"))
                validate_jsonrpc_object_only(payload)

    def test_initialize_fixture_declares_minimal_client_capabilities_and_mcp_version(self) -> None:
        initialize = json.loads((MESSAGE_ROOT / "initialize_request.json").read_text(encoding="utf-8"))
        params = initialize["params"]

        self.assertEqual(params["protocolVersion"], MCP_PROTOCOL_VERSION)
        self.assertEqual(params["capabilities"], {})
        self.assertNotIn("tasks", params["capabilities"])
        self.assertNotIn("roots", params["capabilities"])
        self.assertNotIn("sampling", params["capabilities"])
        self.assertNotIn("elicitation", params["capabilities"])

    def test_error_table_uses_stable_mcp_runtime_prefix_and_required_shape(self) -> None:
        table = json.loads((CONTRACT_ROOT / "typed_errors.json").read_text(encoding="utf-8"))
        codes = []
        for item in table["errors"]:
            with self.subTest(code=item.get("code")):
                self.assertRegex(item["code"], r"^mcp_runtime_[a-z0-9_]+$")
                self.assertIn(item["category"], {"protocol", "transport", "timeout", "capability", "remote", "storage", "lifecycle"})
                self.assertIsInstance(item["retriable"], bool)
                codes.append(item["code"])
        self.assertEqual(len(codes), len(set(codes)))

    def test_frontend_event_schema_accepts_safe_payload_and_rejects_raw_fields(self) -> None:
        schema = json.loads((CONTRACT_ROOT / "frontend_events.schema.json").read_text(encoding="utf-8"))
        validator = Draft202012Validator(schema)
        safe_event = {
            "event_type": "mcp.long_task_progress",
            "payload": {
                "server_id": "crm",
                "tool_name": "search_customer",
                "safe_ref": "mcp-task:crm:search_customer:00000000000000000000000000000001",
                "progress": 10,
                "total": 100,
                "message": "working",
            },
        }
        raw_event = {
            "event_type": "mcp.long_task_progress",
            "payload": {
                "safe_ref": "mcp-task:crm:search_customer:00000000000000000000000000000001",
                "mcp_task_id": "raw-task",
            },
        }

        validator.validate(safe_event)
        self.assertTrue(list(validator.iter_errors(raw_event)))

    def test_sidecar_contract_separates_internal_protocol_from_external_mcp_version(self) -> None:
        contract = json.loads((CONTRACT_ROOT / "sidecar_v1_contract.json").read_text(encoding="utf-8"))

        self.assertEqual(contract["component"], "maf_mcp_runtime_sidecar")
        self.assertEqual(contract["sidecar_protocol_version"], "maf.mcp.sidecar.v1")
        self.assertEqual(contract["external_mcp_protocol_version"], MCP_PROTOCOL_VERSION)
        self.assertEqual(contract["external_mcp_protocol_versions"], [MCP_PROTOCOL_VERSION])
        self.assertEqual(contract["external_mcp_protocol_version_scope"], "single_latest_long_task_phase_baseline")
        self.assertEqual(
            contract["python_visible_mcp_client_protocol_versions"],
            list(SUPPORTED_MCP_PROTOCOL_VERSION_ORDER[:-1]),
        )
        self.assertFalse(contract["canonical_multi_version_transport"])
        self.assertNotEqual(contract["sidecar_protocol_version"], contract["external_mcp_protocol_version"])
        self.assertIn("health", contract["implemented_features"])
        self.assertIn("compatibility_handshake", contract["implemented_features"])
        self.assertIn("multi_version_transport", contract["reserved_features"])
        self.assertIn("mcp_tasks", contract["reserved_features"])
        self.assertIn("remote_cancel", contract["reserved_features"])
        self.assertNotIn("mcp_tasks", contract["implemented_features"])

    def test_conformance_matrix_declares_five_client_versions_and_safe_gates(self) -> None:
        matrix = json.loads((CONTRACT_ROOT / "conformance_matrix.json").read_text(encoding="utf-8"))

        self.assertEqual(matrix["schema_version"], "maf.mcp.client_compatibility_conformance_matrix.v1")
        self.assertEqual(matrix["supported_mcp_spec_versions"], list(SUPPORTED_MCP_PROTOCOL_VERSION_ORDER))
        self.assertNotIn("mcp_spec_version", matrix)
        item_ids = {item["id"] for item in matrix["items"]}
        self.assertIn("MCP-CONF-CLIENT-2024-TRANSPORT", item_ids)
        self.assertIn("MCP-CONF-CLIENT-2025-TRANSPORT", item_ids)
        self.assertIn("MCP-CONF-BATCH-REJECTION", item_ids)
        self.assertIn("MCP-CONF-SAFE-DIAGNOSTICS", item_ids)

    def test_contract_artifacts_do_not_contain_secret_sample_values(self) -> None:
        encoded_contracts = {
            path.name: json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(CONTRACT_ROOT.glob("*.json"))
        }
        try:
            assert_no_raw_sensitive_values(
                encoded_contracts,
                {
                    "auth": "Bearer secret-token",
                    "session": "sess-raw-1",
                    "event": "evt-raw-1",
                    "endpoint": "https://mcp.internal.example/rpc?token=secret",
                },
            )
        except MCPFixtureError as exc:
            self.fail(str(exc))


if __name__ == "__main__":
    unittest.main()
