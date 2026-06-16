from __future__ import annotations

import importlib
import json
import tomllib
import unittest
from pathlib import Path

from src.integrations.rust_safety_contract import load_safety_contract


class SafetyKernelsPyo3WheelContractTest(unittest.TestCase):
    def test_safety_pyo3_wheel_crate_is_workspace_member(self) -> None:
        workspace = tomllib.loads(Path("native/Cargo.toml").read_text(encoding="utf-8"))
        self.assertIn("crates/maf_safety_kernels_pyo3", workspace["workspace"]["members"])

        manifest_path = Path("native/crates/maf_safety_kernels_pyo3/Cargo.toml")
        self.assertTrue(manifest_path.exists())
        manifest = tomllib.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(manifest["package"]["name"], "maf_safety_kernels_pyo3")
        self.assertEqual(manifest["lib"]["name"], "maf_safety_kernels_pyo3")
        self.assertEqual(manifest["lib"]["crate-type"], ["rlib", "cdylib"])
        self.assertIs(manifest["lib"]["test"], False)
        self.assertIn("abi3-py313", manifest["dependencies"]["pyo3"]["features"])
        self.assertNotIn("extension-module", manifest["dependencies"]["pyo3"]["features"])
        for crate in ["maf_artifact_store", "maf_auth_core", "maf_data_access", "maf_audit_sanitizer"]:
            self.assertIn(crate, manifest["dependencies"])

    def test_safety_pyo3_source_exposes_contract_and_kernel_functions(self) -> None:
        source = Path("native/crates/maf_safety_kernels_pyo3/src/lib.rs").read_text(encoding="utf-8")

        for required in [
            "#[pymodule]",
            "fn maf_safety_kernels_pyo3",
            "fn contract_json",
            "fn normalize_storage_key_json",
            "fn sha256_hex_bytes",
            "fn verify_token_json",
            "fn hmac_sha256_hex_json",
            "fn ensure_readonly_sql_json",
            "fn validate_shape_json",
            "fn sanitize_value_json",
            "maf_audit_sanitizer::safety_contract_json",
        ]:
            self.assertIn(required, source)

    def test_installed_pyo3_module_matches_safety_contract_when_available(self) -> None:
        try:
            module = importlib.import_module("maf_safety_kernels_pyo3")
        except ModuleNotFoundError:
            self.skipTest("maf_safety_kernels_pyo3 wheel is not installed in this Python environment")

        self.assertEqual(json.loads(module.contract_json()), load_safety_contract())

        normalized = json.loads(module.normalize_storage_key_json(json.dumps({"key": "task/report.csv"})))
        self.assertEqual(normalized["value"], "task/report.csv")
        self.assertIsNone(normalized["error"])

        denied = json.loads(module.ensure_readonly_sql_json(json.dumps({"sql": "DELETE FROM users"})))
        self.assertIs(denied["allowed"], False)
        self.assertEqual(denied["error"]["code"], "data_access_write_denied")

        sanitized = json.loads(module.sanitize_value_json(json.dumps({"value": {"token": "secret", "safe": 1}})))
        self.assertEqual(sanitized["value"]["token"], "[REDACTED]")
        self.assertEqual(sanitized["value"]["safe"], 1)


if __name__ == "__main__":
    unittest.main()
