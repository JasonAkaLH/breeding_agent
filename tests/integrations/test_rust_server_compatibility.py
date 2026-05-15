from __future__ import annotations

import tomllib
import unittest
from pathlib import Path


TARGET_ENV_MARKERS = [
    "Ubuntu 22.04.5 LTS",
    "GNU/Linux 6.8.0-49-generic x86_64",
    "CUDA 12.6",
    "NVIDIA V100",
]


class RustServerCompatibilityTest(unittest.TestCase):
    def test_docs_pin_ubuntu_2204_x86_64_as_production_native_target(self) -> None:
        documents = [
            Path("README.md").read_text(encoding="utf-8"),
            Path("AGENTS.md").read_text(encoding="utf-8"),
            Path("docs/prd/rust/01-Rust工具链构建发布与质量门禁PRD.md").read_text(encoding="utf-8"),
        ]
        for marker in TARGET_ENV_MARKERS:
            self.assertTrue(any(marker in document for document in documents), marker)

    def test_pyo3_wheel_ci_targets_ubuntu_2204_manylinux_not_macos_release(self) -> None:
        workflow = Path(".github/workflows/rust-quality.yml").read_text(encoding="utf-8")
        self.assertIn("runs-on: ubuntu-22.04", workflow)
        self.assertIn("manylinux_2_35", workflow)
        self.assertIn("--auditwheel check", workflow)
        self.assertNotIn("runs-on: macos-14", workflow)

    def test_pyo3_crates_use_stable_python_abi_without_cuda_dependency(self) -> None:
        for manifest_path in [
            Path("native/crates/maf_skill_runtime_pyo3/Cargo.toml"),
            Path("native/crates/maf_core_lifecycle_pyo3/Cargo.toml"),
        ]:
            manifest = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
            pyo3 = manifest["dependencies"]["pyo3"]
            self.assertIn("abi3-py313", pyo3["features"])
            self.assertNotIn("extension-module", pyo3["features"])
            self.assertEqual(manifest["lib"]["crate-type"], ["rlib", "cdylib"])

        workspace = tomllib.loads(Path("native/Cargo.toml").read_text(encoding="utf-8"))
        dependency_names = set(workspace["workspace"]["dependencies"])
        for forbidden in ["cuda", "cudarc", "nvidia", "tch", "torch-sys"]:
            self.assertNotIn(forbidden, dependency_names)

    def test_native_workspace_has_no_macos_only_cfg_in_first_party_rust_sources(self) -> None:
        first_party_sources = [
            path
            for path in Path("native/crates").rglob("*.rs")
            if "target" not in path.parts
        ]
        joined = "\n".join(path.read_text(encoding="utf-8") for path in first_party_sources)
        self.assertNotIn('target_os = "macos"', joined)
        self.assertNotIn("target_os = 'macos'", joined)
        self.assertNotIn("aarch64-apple-darwin", joined)
        self.assertIn("#[cfg(unix)]", joined)
