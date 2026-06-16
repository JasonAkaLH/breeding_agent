from __future__ import annotations

import json
import sys
import types
import unittest
from typing import Any
from unittest.mock import patch

from src.core.errors import RustCoreContractError
from src.core.rust_contract import load_core_contract


class CorePyo3FacadeTest(unittest.TestCase):
    def tearDown(self) -> None:
        load_core_contract.cache_clear()
        sys.modules.pop("fake_maf_core_lifecycle_pyo3", None)
        sys.modules.pop("bad_maf_core_lifecycle_pyo3", None)

    def test_enforce_requires_prebuilt_pyo3_module(self) -> None:
        load_core_contract.cache_clear()

        with patch.dict(
            "os.environ",
            {
                "MAF_RUST_CORE_MODE": "enforce",
                "MAF_CORE_LIFECYCLE_PYO3_MODULE": "missing_maf_core_lifecycle_pyo3",
            },
            clear=False,
        ):
            with self.assertRaisesRegex(RustCoreContractError, "prebuilt PyO3"):
                load_core_contract()

    def test_enforce_fails_closed_on_pyo3_contract_mismatch(self) -> None:
        module = types.ModuleType("bad_maf_core_lifecycle_pyo3")
        contract = _core_contract()
        contract["schema_hash"] = "wrong"
        module.core_contract_json = lambda: json.dumps(contract)
        sys.modules[module.__name__] = module

        with patch.dict(
            "os.environ",
            {
                "MAF_RUST_CORE_MODE": "enforce",
                "MAF_CORE_LIFECYCLE_PYO3_MODULE": module.__name__,
            },
            clear=False,
        ):
            with self.assertRaisesRegex(RustCoreContractError, "contract mismatch"):
                load_core_contract()

    def test_shadow_keeps_checked_in_artifact_when_pyo3_module_is_absent(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "MAF_RUST_CORE_MODE": "shadow",
                "MAF_CORE_LIFECYCLE_PYO3_MODULE": "missing_maf_core_lifecycle_pyo3",
            },
            clear=False,
        ):
            contract = load_core_contract()

        self.assertEqual(contract["component"], "maf_core_types")
        self.assertEqual(contract["contract_version"], "core.v1")

    def test_shadow_keeps_checked_in_artifact_on_pyo3_contract_mismatch(self) -> None:
        module = types.ModuleType("bad_maf_core_lifecycle_pyo3")
        contract = _core_contract()
        contract["schema_hash"] = "wrong"
        module.core_contract_json = lambda: json.dumps(contract)
        sys.modules[module.__name__] = module

        with patch.dict(
            "os.environ",
            {
                "MAF_RUST_CORE_MODE": "shadow",
                "MAF_CORE_LIFECYCLE_PYO3_MODULE": module.__name__,
            },
            clear=False,
        ):
            loaded = load_core_contract()

        self.assertEqual(loaded["schema_hash"], _core_contract()["schema_hash"])

    def test_enforce_accepts_matching_pyo3_contract(self) -> None:
        module = _fake_module()
        sys.modules[module.__name__] = module

        with patch.dict(
            "os.environ",
            {
                "MAF_RUST_CORE_MODE": "enforce",
                "MAF_CORE_LIFECYCLE_PYO3_MODULE": module.__name__,
            },
            clear=False,
        ):
            contract = load_core_contract()

        self.assertEqual(contract, _core_contract())


def _fake_module() -> types.ModuleType:
    module = types.ModuleType("fake_maf_core_lifecycle_pyo3")
    module.core_contract_json = lambda: json.dumps(_core_contract())
    return module


def _core_contract() -> dict[str, Any]:
    load_core_contract.cache_clear()
    with patch.dict("os.environ", {"MAF_RUST_CORE_MODE": "off"}, clear=False):
        return dict(load_core_contract())


if __name__ == "__main__":
    unittest.main()
