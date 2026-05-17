from __future__ import annotations

import unittest
from pathlib import Path


WORKFLOW = Path(".github/workflows/rust-quality.yml")


class RustQualityWorkflowTest(unittest.TestCase):
    def test_runtime_sidecar_binary_release_evidence_is_uploaded(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")

        for required in (
            "Build maf-runtime-sidecar release binary",
            "Generate RuntimeSidecar binary SBOM / provenance / manifest",
            "Upload RuntimeSidecar binary release evidence",
            "cargo build --release -p maf_runtime_sidecar --bin maf-runtime-sidecar",
            "--component maf_runtime_sidecar",
            "--artifact-id maf_runtime_sidecar",
            "--artifact-kind sidecar_binary",
            "--contract-hash runtime_sidecar=maf_runtime_v1_schema_20260515_edge_artifact",
            "--proto-hash runtime=maf_runtime_proto_v1_20260515_edge_artifact",
            "rust-runtime-sidecar-linux-x86_64",
        ):
            self.assertIn(required, workflow)

    def test_rust_prd_and_prd03_evidence_changes_trigger_quality_gates(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")

        self.assertIn('"docs/prd/rust/**"', workflow)
        self.assertIn('"scripts/validate_prd03_runtime_sidecar_evidence.py"', workflow)

    def test_skill_sandbox_binary_release_evidence_is_uploaded(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")

        for required in (
            "Build maf-skill-sandbox release binary",
            "Generate Skill Sandbox binary SBOM / provenance / manifest",
            "Upload Skill Sandbox binary release evidence",
            "cargo build --release -p maf_skill_runtime --bin maf-skill-sandbox",
            "--component maf_skill_runtime",
            "--artifact-id maf_skill_sandbox",
            "--artifact-kind sidecar_binary",
            "--contract-hash skill_runtime=maf_skill_runtime_schema_gates_20260515",
            "--proto-hash skill=maf_skill_proto_v1_20260515",
            "rust-skill-sandbox-linux-x86_64",
        ):
            self.assertIn(required, workflow)

    def test_prd04_evidence_changes_trigger_quality_gates(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")

        self.assertIn('"scripts/validate_prd04_skill_runtime_evidence.py"', workflow)
        self.assertIn('"docs/prd/rust/**"', workflow)

    def test_mcp_runtime_sidecar_binary_release_evidence_is_uploaded(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")

        for required in (
            "Build maf-mcp-runtime-sidecar release binary",
            "Generate MCP Runtime sidecar binary SBOM / provenance / manifest",
            "Upload MCP Runtime sidecar binary release evidence",
            "cargo build --release -p maf_mcp_runtime --bin maf-mcp-runtime-sidecar",
            "--component maf_mcp_runtime",
            "--artifact-id maf_mcp_runtime_sidecar",
            "--artifact-kind sidecar_binary",
            "--contract-hash mcp_runtime=maf_mcp_v1_phase1_schema_hash_pending_ci",
            "--contract-hash mcp_runtime_errors=maf_mcp_runtime_error_table_v1_phase1",
            "--proto-hash mcp=maf_mcp_proto_v1_20260517",
            "rust-mcp-runtime-linux-x86_64",
        ):
            self.assertIn(required, workflow)

    def test_prd05_evidence_changes_trigger_quality_gates(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")

        self.assertIn('"scripts/validate_prd05_mcp_runtime_evidence.py"', workflow)
        self.assertIn('"src/integrations/mcp/**"', workflow)
        self.assertIn('"docs/prd/rust/**"', workflow)
