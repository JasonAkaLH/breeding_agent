from __future__ import annotations

import json
import unittest
from pathlib import Path

from src.integrations.mcp.mcp_runtime_gates import validate_mcp_official_sdk_conformance_matrix
from src.integrations.mcp.protocol import SUPPORTED_MCP_PROTOCOL_VERSION_ORDER


class OfficialSDKConformanceMatrixTest(unittest.TestCase):
    def test_matrix_declares_exact_four_versions_and_adapter_transport_results(self) -> None:
        matrix = json.loads(Path("tests/fixtures/mcp/contracts/conformance_matrix.json").read_text(encoding="utf-8"))

        result = validate_mcp_official_sdk_conformance_matrix(matrix)

        self.assertEqual(result["supported_mcp_spec_versions"], ",".join(SUPPORTED_MCP_PROTOCOL_VERSION_ORDER))
        self.assertEqual(result["adapters"], "official_rust_sdk,python_legacy")
        self.assertEqual(result["transport_scope"], "remote_http_only_until_stdio_sandbox_passes")
        official_2024 = matrix["adapter_conformance"]["2024-11-05"]["legacy_http_sse"]["official_rust_sdk"]
        self.assertEqual(official_2024["operational_status"], "unsupported_transport")
        self.assertEqual(official_2024["shadow_compare"], "skipped")
        self.assertFalse(official_2024["enforce_allowed"])
        official_2025 = matrix["adapter_conformance"]["2025-11-25"]["streamable_http"]["official_rust_sdk"]
        self.assertEqual(official_2025["operational_status"], "partial_shadow_verified")
        self.assertTrue(official_2025["object_response"])
        self.assertFalse(official_2025["sse_response"])
        self.assertTrue(official_2025["sse_response_gap_reason"])

    def test_matrix_rejects_silent_version_expansion_or_missing_adapter_result(self) -> None:
        matrix = json.loads(Path("tests/fixtures/mcp/contracts/conformance_matrix.json").read_text(encoding="utf-8"))
        expanded = dict(matrix)
        expanded["supported_mcp_spec_versions"] = [*SUPPORTED_MCP_PROTOCOL_VERSION_ORDER, "2026-01-01"]
        with self.assertRaisesRegex(RuntimeError, "mcp_runtime_conformance_blocked"):
            validate_mcp_official_sdk_conformance_matrix(expanded)

        missing_adapter = json.loads(Path("tests/fixtures/mcp/contracts/conformance_matrix.json").read_text(encoding="utf-8"))
        del missing_adapter["adapter_conformance"]["2025-03-26"]["streamable_http"]["official_rust_sdk"]
        with self.assertRaisesRegex(RuntimeError, "mcp_runtime_conformance_blocked"):
            validate_mcp_official_sdk_conformance_matrix(missing_adapter)

    def test_matrix_rejects_stdio_scope_and_shadow_mismatch_as_enforce_input(self) -> None:
        matrix = json.loads(Path("tests/fixtures/mcp/contracts/conformance_matrix.json").read_text(encoding="utf-8"))
        matrix["transport_scope"] = "all_transports"
        matrix["stdio_sandbox_conformance_passed"] = True
        with self.assertRaisesRegex(RuntimeError, "mcp_runtime_conformance_blocked"):
            validate_mcp_official_sdk_conformance_matrix(matrix)

        mismatch = json.loads(Path("tests/fixtures/mcp/contracts/conformance_matrix.json").read_text(encoding="utf-8"))
        mismatch["adapter_conformance"]["2025-11-25"]["streamable_http"]["official_rust_sdk"][
            "shadow_compare"
        ] = "mismatched"
        with self.assertRaisesRegex(RuntimeError, "mcp_runtime_conformance_blocked"):
            validate_mcp_official_sdk_conformance_matrix(mismatch)

        overclaimed = json.loads(Path("tests/fixtures/mcp/contracts/conformance_matrix.json").read_text(encoding="utf-8"))
        overclaimed["adapter_conformance"]["2024-11-05"]["legacy_http_sse"]["official_rust_sdk"][
            "initialize"
        ] = True
        with self.assertRaisesRegex(RuntimeError, "mcp_runtime_conformance_blocked"):
            validate_mcp_official_sdk_conformance_matrix(overclaimed)

    def test_matrix_requires_existing_evidence_refs_for_adapter_results(self) -> None:
        matrix = json.loads(Path("tests/fixtures/mcp/contracts/conformance_matrix.json").read_text(encoding="utf-8"))
        missing_refs = json.loads(json.dumps(matrix))
        del missing_refs["adapter_conformance"]["2025-11-25"]["streamable_http"]["python_legacy"]["evidence_refs"]
        with self.assertRaisesRegex(RuntimeError, "mcp_runtime_conformance_blocked"):
            validate_mcp_official_sdk_conformance_matrix(missing_refs)

        bad_ref = json.loads(json.dumps(matrix))
        bad_ref["adapter_conformance"]["2025-11-25"]["streamable_http"]["official_rust_sdk"][
            "evidence_refs"
        ] = ["tests/integrations/mcp/does_not_exist.py::test_missing"]
        with self.assertRaisesRegex(RuntimeError, "mcp_runtime_conformance_blocked"):
            validate_mcp_official_sdk_conformance_matrix(bad_ref)


if __name__ == "__main__":
    unittest.main()
