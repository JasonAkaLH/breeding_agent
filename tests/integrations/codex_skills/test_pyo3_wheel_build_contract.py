from __future__ import annotations

import importlib
import json
import tomllib
import unittest
from pathlib import Path

from src.integrations.codex_skills.rust_contract import load_skill_runtime_contract


class SkillRuntimePyo3WheelBuildContractTest(unittest.TestCase):
    def test_pyo3_wheel_crate_is_workspace_member_without_breaking_workspace_tests(self) -> None:
        workspace = tomllib.loads(Path("native/Cargo.toml").read_text(encoding="utf-8"))
        self.assertIn("crates/maf_skill_runtime_pyo3", workspace["workspace"]["members"])

        manifest_path = Path("native/crates/maf_skill_runtime_pyo3/Cargo.toml")
        self.assertTrue(manifest_path.exists())
        manifest = tomllib.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(manifest["package"]["name"], "maf_skill_runtime_pyo3")
        self.assertEqual(manifest["lib"]["name"], "maf_skill_runtime_pyo3")
        self.assertEqual(manifest["lib"]["crate-type"], ["rlib", "cdylib"])
        self.assertIs(manifest["lib"]["test"], False)
        self.assertIn("abi3-py313", manifest["dependencies"]["pyo3"]["features"])
        self.assertNotIn("extension-module", manifest["dependencies"]["pyo3"]["features"])
        self.assertEqual(manifest["dependencies"]["maf_skill_runtime"]["path"], "../maf_skill_runtime")

    def test_pyo3_wheel_source_exposes_policy_contract_functions(self) -> None:
        source = Path("native/crates/maf_skill_runtime_pyo3/src/lib.rs").read_text(encoding="utf-8")

        self.assertIn("#[pymodule]", source)
        self.assertIn("fn maf_skill_runtime_pyo3", source)
        self.assertIn("fn contract_json", source)
        self.assertIn("fn validate_policy_json", source)
        self.assertIn("skill_runtime_contract_json", source)
        self.assertIn("skill_policy_validate_json", source)

    def test_installed_pyo3_module_matches_rust_contract_when_available(self) -> None:
        try:
            module = importlib.import_module("maf_skill_runtime_pyo3")
        except ModuleNotFoundError:
            self.skipTest("maf_skill_runtime_pyo3 wheel is not installed in this Python environment")

        contract = json.loads(module.contract_json())
        self.assertEqual(contract, load_skill_runtime_contract())
        allowed = json.loads(
            module.validate_policy_json(
                json.dumps(
                    {
                        "skill_name": "example",
                        "capability_id": "skill.example",
                        "execution_mode": "platform_service",
                        "trust_scope": "project",
                        "handler": "skill.example.platform_handler",
                        "manifest_services": ["llm.generate"],
                        "runtime_allowlist_services": ["llm.generate"],
                        "requested_services": ["llm.generate"],
                        "runtime_allowlist_handlers": ["skill.example.platform_handler"],
                        "x_runtime_rust": {"adapter": "pyo3", "contract_version": "1"},
                    },
                    sort_keys=True,
                )
            )
        )
        self.assertIs(allowed["allowed"], True)
        self.assertIsNone(allowed["error"])

        denied = json.loads(module.validate_policy_json("{not-json"))
        self.assertIs(denied["allowed"], False)
        self.assertEqual(denied["error"]["code"], "skill_runtime_contract_mismatch")
