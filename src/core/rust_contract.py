from __future__ import annotations

import importlib
import json
import os
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

from .errors import RustCoreContractError

_CONTRACT_PATH = Path(__file__).with_name("rust_contracts") / "core_contract.json"
_MODE_ENV = "MAF_RUST_CORE_MODE"
_MODULE_ENV = "MAF_CORE_LIFECYCLE_PYO3_MODULE"
_DEFAULT_MODULE_NAME = "maf_core_lifecycle_pyo3"
_VALID_MODES = frozenset({"off", "shadow", "enforce"})
_REQUIRED_FEATURES = frozenset({"core_contract_artifact", "pyo3_core_facade"})


def load_core_contract() -> dict[str, Any]:
    """Load the Rust-owned core contract artifact.

    `maf_core_types` is the canonical source for enum values, model field
    snapshots, and stable core error-code metadata. Python keeps this tiny
    loader so existing import paths can remain stable without re-defining the
    contract in Python.
    """

    return _load_core_contract_for_mode(rust_core_mode(), _pyo3_module_name())


@lru_cache(maxsize=8)
def _load_core_contract_for_mode(mode: str, module_name: str) -> dict[str, Any]:
    checked_in_contract = _load_checked_in_contract()
    if mode == "off":
        return checked_in_contract

    try:
        pyo3_contract = _load_pyo3_core_contract(module_name)
    except ModuleNotFoundError as exc:
        if mode == "enforce":
            raise RustCoreContractError(
                "Core enforce mode requires a prebuilt PyO3 module; runtime import/build fallback is forbidden",
                safe_metadata={"mode": mode, "module": module_name},
            ) from exc
        return checked_in_contract
    except RustCoreContractError:
        if mode == "enforce":
            raise
        return checked_in_contract

    try:
        _ensure_core_contract_compatible(pyo3_contract, checked_in_contract)
    except RustCoreContractError:
        if mode == "enforce":
            raise
        return checked_in_contract
    if mode == "enforce":
        return pyo3_contract
    return checked_in_contract


def rust_core_mode() -> str:
    mode = os.environ.get(_MODE_ENV, "off").strip().lower()
    if mode not in _VALID_MODES:
        raise RustCoreContractError(
            f"Invalid {_MODE_ENV}: {mode}",
            code="core_contract_validation_failed",
            safe_metadata={"mode": mode},
        )
    return mode


def core_enum_members(enum_name: str) -> dict[str, str]:
    members = load_core_contract()["enums"].get(enum_name)
    if members is None:
        raise KeyError(f"Unknown Rust core enum contract: {enum_name}")
    return {str(member["name"]): str(member["value"]) for member in members}


def core_model_fields(model_name: str) -> list[str]:
    fields = load_core_contract()["models"].get(model_name)
    if fields is None:
        raise KeyError(f"Unknown Rust core model contract: {model_name}")
    return [str(field) for field in fields]


def _load_checked_in_contract() -> dict[str, Any]:
    with _CONTRACT_PATH.open("r", encoding="utf-8") as handle:
        contract = json.load(handle)
    if not isinstance(contract, Mapping):
        raise RustCoreContractError("core contract artifact is not a JSON object")
    contract = dict(contract)
    if contract.get("component") != "maf_core_types":
        raise RustCoreContractError("core contract artifact component mismatch")
    if not isinstance(contract.get("supported_features"), list):
        raise RustCoreContractError("core contract artifact missing supported_features")
    return contract


def _load_pyo3_core_contract(module_name: str) -> dict[str, Any]:
    module = importlib.import_module(module_name)
    contract_json = getattr(module, "core_contract_json", None)
    if not callable(contract_json):
        raise RustCoreContractError("Core PyO3 module is missing core_contract_json entrypoint")
    return _parse_json_mapping(contract_json(), "Core PyO3 contract")


def _pyo3_module_name() -> str:
    return os.environ.get(_MODULE_ENV, "").strip() or _DEFAULT_MODULE_NAME


def _ensure_core_contract_compatible(actual: Mapping[str, Any], expected: Mapping[str, Any]) -> None:
    for key in ("component", "contract_version", "schema_hash", "error_code_table_hash"):
        if actual.get(key) != expected.get(key):
            raise RustCoreContractError(
                "Core PyO3 contract mismatch",
                safe_metadata={"field": key},
            )
    actual_features = {str(feature) for feature in actual.get("supported_features", ())}
    expected_features = {str(feature) for feature in expected.get("supported_features", ())}
    required_features = expected_features | _REQUIRED_FEATURES
    if not required_features.issubset(actual_features):
        raise RustCoreContractError(
            "Core PyO3 contract mismatch",
            safe_metadata={"field": "supported_features"},
        )


def _parse_json_mapping(raw: Any, label: str) -> dict[str, Any]:
    try:
        parsed = json.loads(str(raw))
    except (TypeError, json.JSONDecodeError) as exc:
        raise RustCoreContractError(f"{label} is not valid JSON") from exc
    if not isinstance(parsed, Mapping):
        raise RustCoreContractError(f"{label} is not a JSON object")
    return dict(parsed)


load_core_contract.cache_clear = _load_core_contract_for_mode.cache_clear  # type: ignore[attr-defined]


__all__ = ["core_enum_members", "core_model_fields", "load_core_contract", "rust_core_mode"]
