from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SUPPORTED_MARKER = "2024-11-05 / 2025-03-26 / 2025-06-18 / 2025-11-25"


class MCPPRDDDocumentSyncTests(unittest.TestCase):
    def test_mcp_index_exposes_client_multi_version_compatibility_track(self) -> None:
        text = (ROOT / "docs/prd/MCP/README.md").read_text(encoding="utf-8")

        self.assertIn("docs/prd/MCP/compatibility/", text)
        self.assertIn("client multi-version compatibility", text)
        self.assertIn(SUPPORTED_MARKER, text)

    def test_joint_overview_separates_latest_feature_and_multi_version_invariants(self) -> None:
        text = (ROOT / "docs/prd/MCP/00-MCPRuntime联合改造总览PRD.md").read_text(encoding="utf-8")

        self.assertIn("latest-feature invariant", text)
        self.assertIn("multi-version client compatibility invariant", text)
        self.assertIn("2025 session-era invariant", text)
        self.assertIn(SUPPORTED_MARKER, text)

    def test_backend_runtime_prd_points_to_compatibility_matrix_not_single_latest_only(self) -> None:
        text = (ROOT / "docs/prd/backend/14-MCPRuntime实现需求PRD.md").read_text(encoding="utf-8")

        self.assertIn("四版本 client compatibility matrix", text)
        self.assertIn("docs/prd/MCP/compatibility/README.md", text)
        self.assertIn(SUPPORTED_MARKER, text)
        self.assertIn("2025-11-25 latest features", text)

    def test_long_task_and_rust_sidecar_docs_keep_latest_feature_boundary(self) -> None:
        long_task = (ROOT / "docs/prd/backend/17-MCP长任务流式SSEPRD.md").read_text(encoding="utf-8")
        rust_sidecar = (ROOT / "docs/prd/rust/05-MCPRuntimeRustSidecarPRD.md").read_text(encoding="utf-8")

        self.assertIn("2025-11-25 latest-feature", long_task)
        self.assertIn("不是四版本普通 tools 兼容的首版目标", long_task)
        self.assertIn("canonical multi-version transport 仍需后续 feature evidence", rust_sidecar)
        self.assertIn("Python MCP client path", rust_sidecar)


if __name__ == "__main__":
    unittest.main()
