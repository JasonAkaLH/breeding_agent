from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path


class RustQualityGateTest(unittest.TestCase):
    def test_quality_gate_runner_exposes_prd01_command_matrix(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "scripts/run_rust_quality_gates.py",
                "--plan-json",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        plan = json.loads(result.stdout)
        gates = {gate["name"]: gate for gate in plan["gates"]}

        for name in [
            "cargo_fmt",
            "cargo_clippy",
            "cargo_test",
            "cargo_nextest",
            "cargo_audit",
            "cargo_deny",
            "cargo_llvm_cov",
            "rust_coverage_thresholds",
            "fuzz_cargo_check",
            "cargo_fuzz_smoke",
            "rust_artifact_provenance_self_check",
            "skill_runtime_pyo3_wheel_smoke",
            "fuzz_target_manifest",
        ]:
            self.assertIn(name, gates)

        self.assertEqual(gates["cargo_fmt"]["command"], ["cargo", "fmt", "--check"])
        self.assertEqual(
            gates["cargo_clippy"]["command"],
            [
                "cargo",
                "clippy",
                "--workspace",
                "--all-targets",
                "--all-features",
                "--",
                "-D",
                "warnings",
            ],
        )
        self.assertEqual(gates["cargo_test"]["command"], ["cargo", "test", "--workspace", "--all-features"])
        self.assertEqual(gates["cargo_nextest"]["command"], ["cargo", "nextest", "run", "--workspace", "--all-features"])
        self.assertEqual(gates["cargo_audit"]["command"], ["cargo", "audit"])
        self.assertEqual(gates["cargo_deny"]["command"], ["cargo", "deny", "check"])
        self.assertIn("llvm-cov", gates["cargo_llvm_cov"]["command"])
        self.assertEqual(
            gates["rust_coverage_thresholds"]["command"],
            [sys.executable, "scripts/run_rust_coverage_thresholds.py", "--run"],
        )
        self.assertIn("native/fuzz/Cargo.toml", gates["fuzz_cargo_check"]["command"])
        self.assertEqual(
            gates["cargo_fuzz_smoke"]["command"],
            ["cargo", "fuzz", "run", "skill_runtime_policy", "--", "-max_total_time=30"],
        )
        self.assertEqual(
            gates["rust_artifact_provenance_self_check"]["command"],
            [sys.executable, "scripts/rust_artifact_provenance.py", "self-test"],
        )
        self.assertIn("maturin", gates["skill_runtime_pyo3_wheel_smoke"]["command"])
        self.assertIn("--compatibility", gates["skill_runtime_pyo3_wheel_smoke"]["command"])
        self.assertIn("manylinux_2_35", gates["skill_runtime_pyo3_wheel_smoke"]["command"])
        self.assertIn("--auditwheel", gates["skill_runtime_pyo3_wheel_smoke"]["command"])
        self.assertEqual(plan["workspace"], "native")
        self.assertEqual(plan["python_env"], "multi_agent")

    def test_github_workflow_enforces_prd01_rust_quality_gates(self) -> None:
        workflow = Path(".github/workflows/rust-quality.yml")
        self.assertTrue(workflow.exists())
        text = workflow.read_text(encoding="utf-8")

        for required in [
            "cargo fmt --check",
            "cargo clippy --workspace --all-targets --all-features -- -D warnings",
            "cargo test --workspace --all-features",
            "cargo nextest run --workspace --all-features",
            "cargo audit",
            "cargo deny check",
            "cargo llvm-cov --workspace --all-features --summary-only",
            "python scripts/run_rust_coverage_thresholds.py --run",
            "python scripts/rust_artifact_provenance.py self-test",
            "cargo metadata --locked --format-version 1 --manifest-path native/Cargo.toml",
            "mapfile -t WHEELS",
            '"${#WHEELS[@]}" -ne 1',
            'RUSTC_VERSION="$(rustc --version)"',
            '--toolchain "$RUSTC_VERSION"',
            "python scripts/rust_artifact_provenance.py write-sbom",
            "python scripts/rust_artifact_provenance.py write-provenance",
            "python scripts/rust_artifact_provenance.py generate",
            "python -m maturin build --release --manifest-path native/crates/maf_skill_runtime_pyo3/Cargo.toml --compatibility manylinux_2_35 --auditwheel check",
            "test_installed_pyo3_module_matches_rust_contract_when_available",
            "actions/upload-artifact@v4",
            "runs-on: ubuntu-22.04",
            "Ubuntu 22.04 x86_64",
            "branches: [main, rust_branch]",
        ]:
            self.assertIn(required, text)

        for tool in ["cargo-nextest", "cargo-audit", "cargo-deny", "cargo-llvm-cov"]:
            self.assertIn(tool, text)
        self.assertIn("CARGO_BUILD_JOBS: \"1\"", text)
        self.assertIn("Python 3.13", text)

    def test_docs_reference_quality_gate_runner_and_non_default_wheel_smoke(self) -> None:
        readme = Path("README.md").read_text(encoding="utf-8")
        agents = Path("AGENTS.md").read_text(encoding="utf-8")

        for document in [readme, agents]:
            self.assertIn("scripts/run_rust_quality_gates.py", document)
            self.assertIn("Skill Runtime PyO3 wheel 本地 smoke", document)
            self.assertIn("Ubuntu 22.04", document)
            self.assertIn("manylinux_2_35", document)
            self.assertIn("非默认回归", document)

    def test_coverage_threshold_runner_encodes_prd01_80_90_policy(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "scripts/run_rust_coverage_thresholds.py",
                "--plan-json",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        plan = json.loads(result.stdout)
        self.assertEqual(plan["workspace_threshold"]["line_coverage"], 80)
        self.assertEqual(plan["security_threshold"]["line_coverage"], 90)
        self.assertIn("--fail-under-lines", plan["workspace_threshold"]["command"])
        self.assertIn("80", plan["workspace_threshold"]["command"])
        for crate in [
            "maf_skill_runtime",
            "maf_mcp_runtime",
            "maf_artifact_store",
            "maf_auth_core",
            "maf_data_access",
            "maf_audit_sanitizer",
        ]:
            self.assertIn(crate, plan["security_threshold"]["crates"])
