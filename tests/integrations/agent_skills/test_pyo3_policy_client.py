from __future__ import annotations

import json
import sys
import types
import unittest
from unittest.mock import patch
from typing import Any

from src.api.runtime import _resolve_skill_policy_client
from src.integrations.agent_skills.pyo3_policy import SkillRuntimePyo3PolicyClient
from src.integrations.agent_skills.rust_contract import load_skill_runtime_contract


class SkillRuntimePyo3PolicyClientTest(unittest.TestCase):
    def tearDown(self) -> None:
        sys.modules.pop("fake_maf_skill_runtime_pyo3", None)
        sys.modules.pop("bad_maf_skill_runtime_pyo3", None)

    def test_pyo3_policy_client_validates_contract_and_calls_rust_policy_json(self) -> None:
        calls: list[dict[str, Any]] = []
        module = _fake_pyo3_module()

        def validate_policy_json(payload: str) -> str:
            calls.append(json.loads(payload))
            return json.dumps({"allowed": True, "bundle_fingerprint": "fp", "error": None})

        module.validate_policy_json = validate_policy_json
        sys.modules[module.__name__] = module

        client = SkillRuntimePyo3PolicyClient(module_name=module.__name__)
        response = client.validate_policy(
            skill_name="platform",
            capability_id="skill.platform",
            execution_mode="platform_service",
            trust_scope="project",
            handler="demo.handler",
            manifest_services=("demo.service",),
            runtime_allowlist_services=("demo.service",),
            requested_services=("demo.service",),
            runtime_allowlist_handlers=("demo.handler",),
            x_runtime_rust={"adapter": "pyo3", "contract_version": "1"},
        )

        self.assertEqual(response, {"allowed": True, "bundle_fingerprint": "fp", "error": None})
        self.assertEqual(calls[0]["skill_name"], "platform")
        self.assertEqual(calls[0]["runtime_allowlist_handlers"], ["demo.handler"])
        self.assertEqual(calls[0]["x_runtime_rust"], {"adapter": "pyo3", "contract_version": "1"})

    def test_pyo3_policy_client_fails_closed_on_contract_mismatch(self) -> None:
        module = types.ModuleType("bad_maf_skill_runtime_pyo3")
        contract = _pyo3_contract()
        contract["schema_hash"] = "wrong"
        module.contract_json = lambda: json.dumps(contract)
        module.validate_policy_json = lambda payload: "{}"
        sys.modules[module.__name__] = module

        with self.assertRaisesRegex(RuntimeError, "contract mismatch"):
            SkillRuntimePyo3PolicyClient(module_name=module.__name__)

    def test_pyo3_policy_client_rejects_malformed_policy_response(self) -> None:
        module = _fake_pyo3_module()
        module.validate_policy_json = lambda payload: "{}"
        sys.modules[module.__name__] = module

        client = SkillRuntimePyo3PolicyClient(module_name=module.__name__)
        with self.assertRaisesRegex(RuntimeError, "invalid policy response"):
            client.validate_policy(
                skill_name="platform",
                capability_id="skill.platform",
                execution_mode="platform_service",
                trust_scope="project",
                handler="demo.handler",
                manifest_services=(),
                runtime_allowlist_services=(),
                requested_services=(),
                runtime_allowlist_handlers=(),
            )

    def test_runtime_policy_client_prefers_prebuilt_pyo3_module_over_sidecar_fallback(self) -> None:
        module = _fake_pyo3_module()
        module.validate_policy_json = lambda payload: json.dumps({"allowed": True, "bundle_fingerprint": "fp", "error": None})
        sys.modules[module.__name__] = module
        fallback = object()

        with patch.dict("os.environ", {"MAF_SKILL_POLICY_PYO3_MODULE": module.__name__}):
            client = _resolve_skill_policy_client(fallback)

        self.assertIsInstance(client, SkillRuntimePyo3PolicyClient)

    def test_runtime_policy_client_uses_sidecar_fallback_when_pyo3_module_is_absent(self) -> None:
        fallback = object()

        with patch.dict("os.environ", {"MAF_SKILL_POLICY_PYO3_MODULE": "missing_maf_skill_runtime_pyo3"}):
            client = _resolve_skill_policy_client(fallback)

        self.assertIs(client, fallback)


def _fake_pyo3_module() -> types.ModuleType:
    module = types.ModuleType("fake_maf_skill_runtime_pyo3")
    module.contract_json = lambda: json.dumps(_pyo3_contract())
    return module


def _pyo3_contract() -> dict[str, Any]:
    contract = dict(load_skill_runtime_contract())
    features = list(contract.get("supported_features", ()))
    if "pyo3_policy_facade" not in features:
        features.append("pyo3_policy_facade")
    contract["supported_features"] = features
    return contract


if __name__ == "__main__":
    unittest.main()
