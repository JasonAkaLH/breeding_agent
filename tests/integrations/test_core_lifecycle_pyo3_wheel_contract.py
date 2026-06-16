from __future__ import annotations

import importlib
import json
import tomllib
import unittest
from pathlib import Path

from src.core.rust_contract import load_core_contract
from src.lifecycle.rust_contract import load_lifecycle_contract


class CoreLifecyclePyo3WheelContractTest(unittest.TestCase):
    def test_core_lifecycle_pyo3_wheel_crate_is_workspace_member(self) -> None:
        workspace = tomllib.loads(Path("native/Cargo.toml").read_text(encoding="utf-8"))
        self.assertIn("crates/maf_core_lifecycle_pyo3", workspace["workspace"]["members"])

        manifest_path = Path("native/crates/maf_core_lifecycle_pyo3/Cargo.toml")
        self.assertTrue(manifest_path.exists())
        manifest = tomllib.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(manifest["package"]["name"], "maf_core_lifecycle_pyo3")
        self.assertEqual(manifest["lib"]["name"], "maf_core_lifecycle_pyo3")
        self.assertEqual(manifest["lib"]["crate-type"], ["rlib", "cdylib"])
        self.assertIs(manifest["lib"]["test"], False)
        self.assertIn("abi3-py313", manifest["dependencies"]["pyo3"]["features"])
        self.assertNotIn("extension-module", manifest["dependencies"]["pyo3"]["features"])
        self.assertEqual(manifest["dependencies"]["maf_core_types"]["path"], "../maf_core_types")
        self.assertEqual(manifest["dependencies"]["maf_lifecycle"]["path"], "../maf_lifecycle")

    def test_core_lifecycle_pyo3_source_exposes_contract_and_transition_functions(self) -> None:
        source = Path("native/crates/maf_core_lifecycle_pyo3/src/lib.rs").read_text(encoding="utf-8")

        for required in [
            "#[pymodule]",
            "fn maf_core_lifecycle_pyo3",
            "pub fn core_contract_json",
            "pub fn lifecycle_contract_json",
            "pub fn lifecycle_can_transition_json",
            "pub fn lifecycle_transition_target_json",
            "pub fn lifecycle_cancel_node_target_json",
            "pub fn lifecycle_can_accept_late_result_json",
            "maf_core_types::core_contract_json",
            "maf_lifecycle::lifecycle_contract_json",
            "maf_lifecycle::can_transition",
        ]:
            self.assertIn(required, source)

    def test_installed_pyo3_module_matches_core_lifecycle_contract_when_available(self) -> None:
        try:
            module = importlib.import_module("maf_core_lifecycle_pyo3")
        except ModuleNotFoundError:
            self.skipTest("maf_core_lifecycle_pyo3 wheel is not installed in this Python environment")

        self.assertEqual(json.loads(module.core_contract_json()), load_core_contract())
        self.assertEqual(json.loads(module.lifecycle_contract_json()), load_lifecycle_contract())

        allowed = json.loads(
            module.lifecycle_can_transition_json(
                json.dumps({"operation": "node.begin_resume", "current": "ready_to_resume"}, sort_keys=True)
            )
        )
        self.assertIs(allowed["allowed"], True)
        self.assertIsNone(allowed["error"])

        malformed = json.loads(module.lifecycle_can_transition_json("{not-json"))
        self.assertIs(malformed["allowed"], False)
        self.assertEqual(malformed["error"]["code"], "lifecycle_structured_output_invalid")


if __name__ == "__main__":
    unittest.main()
