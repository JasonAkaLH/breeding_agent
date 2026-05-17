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
