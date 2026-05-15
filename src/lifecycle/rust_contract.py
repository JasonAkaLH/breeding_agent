from __future__ import annotations

import importlib
import json
import os
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

from .errors import LifecycleRustContractError

_CONTRACT_PATH = Path(__file__).with_name("rust_contracts") / "lifecycle_contract.json"
_MODE_ENV = "MAF_RUST_LIFECYCLE_MODE"
_MODULE_ENV = "MAF_CORE_LIFECYCLE_PYO3_MODULE"
_DEFAULT_MODULE_NAME = "maf_core_lifecycle_pyo3"
_VALID_MODES = frozenset({"off", "shadow", "enforce"})
_REQUIRED_FEATURES = frozenset({"lifecycle_transition_table", "pyo3_lifecycle_facade"})


def load_lifecycle_contract() -> dict[str, Any]:
    """Load the Rust-owned lifecycle transition contract artifact."""

    return _load_lifecycle_contract_for_mode(rust_lifecycle_mode(), _pyo3_module_name())


@lru_cache(maxsize=8)
def _load_lifecycle_contract_for_mode(mode: str, module_name: str) -> dict[str, Any]:
    checked_in_contract = _load_checked_in_contract()
    if mode == "off":
        return checked_in_contract

    try:
        pyo3_contract = _load_pyo3_lifecycle_contract(module_name)
    except ModuleNotFoundError as exc:
        if mode == "enforce":
            raise LifecycleRustContractError(
                "Lifecycle enforce mode requires a prebuilt PyO3 module; runtime import/build fallback is forbidden",
                safe_metadata={"mode": mode, "module": module_name},
            ) from exc
        return checked_in_contract
    except LifecycleRustContractError:
        if mode == "enforce":
            raise
        return checked_in_contract

    try:
        _ensure_lifecycle_contract_compatible(pyo3_contract, checked_in_contract)
    except LifecycleRustContractError:
        if mode == "enforce":
            raise
        return checked_in_contract
    if mode == "enforce":
        return pyo3_contract
    return checked_in_contract


def rust_lifecycle_mode() -> str:
    mode = os.environ.get(_MODE_ENV, "off").strip().lower()
    if mode not in _VALID_MODES:
        raise LifecycleRustContractError(
            f"Invalid {_MODE_ENV}: {mode}",
            code="lifecycle_structured_output_invalid",
            safe_metadata={"mode": mode},
        )
    return mode


def _load_checked_in_contract() -> dict[str, Any]:
    with _CONTRACT_PATH.open("r", encoding="utf-8") as handle:
        contract = json.load(handle)
    if not isinstance(contract, Mapping):
        raise LifecycleRustContractError("lifecycle contract artifact is not a JSON object")
    contract = dict(contract)
    if contract.get("component") != "maf_lifecycle":
        raise LifecycleRustContractError("lifecycle contract artifact component mismatch")
    if not isinstance(contract.get("supported_features"), list):
        raise LifecycleRustContractError("lifecycle contract artifact missing supported_features")
    return contract


def transition_rule(operation: str) -> dict[str, Any]:
    rule = load_lifecycle_contract()["transitions"].get(operation)
    if rule is None:
        raise KeyError(f"Unknown Rust lifecycle transition: {operation}")
    return rule


def transition_allowed(operation: str, current_status: object) -> bool:
    current = str(current_status)
    module = _pyo3_module_for_enforce_call()
    if module is not None:
        response = _call_pyo3_json(
            module,
            "lifecycle_can_transition_json",
            {"current": current, "operation": operation},
            "Lifecycle PyO3 transition response",
        )
        _raise_lifecycle_response_error(response)
        if not isinstance(response.get("allowed"), bool):
            raise LifecycleRustContractError(
                "Lifecycle PyO3 returned invalid transition response",
                code="lifecycle_structured_output_invalid",
            )
        return bool(response["allowed"])
    return current in transition_rule(operation)["from"]


def transition_target(operation: str) -> str:
    module = _pyo3_module_for_enforce_call()
    if module is not None:
        response = _call_pyo3_json(
            module,
            "lifecycle_transition_target_json",
            {"operation": operation},
            "Lifecycle PyO3 transition target response",
        )
        _raise_lifecycle_response_error(response)
        target = response.get("target")
        if not isinstance(target, str):
            raise LifecycleRustContractError(
                "Lifecycle PyO3 returned invalid transition target response",
                code="lifecycle_structured_output_invalid",
            )
        return target
    return str(transition_rule(operation)["to"])


def cancel_node_target(current_status: object) -> str | None:
    module = _pyo3_module_for_enforce_call()
    if module is not None:
        response = _call_pyo3_json(
            module,
            "lifecycle_cancel_node_target_json",
            {"status": str(current_status)},
            "Lifecycle PyO3 cancel-node response",
        )
        _raise_lifecycle_response_error(response)
        target = response.get("target")
        if target is not None and not isinstance(target, str):
            raise LifecycleRustContractError(
                "Lifecycle PyO3 returned invalid cancel-node response",
                code="lifecycle_structured_output_invalid",
            )
        return target
    return load_lifecycle_contract()["cancel_node_targets"].get(str(current_status))


def can_accept_late_result_status(task_status: object | None) -> bool:
    status = None if task_status is None else str(task_status)
    module = _pyo3_module_for_enforce_call()
    if module is not None:
        response = _call_pyo3_json(
            module,
            "lifecycle_can_accept_late_result_json",
            {"task_status": status},
            "Lifecycle PyO3 late-result response",
        )
        _raise_lifecycle_response_error(response)
        if not isinstance(response.get("allowed"), bool):
            raise LifecycleRustContractError(
                "Lifecycle PyO3 returned invalid late-result response",
                code="lifecycle_structured_output_invalid",
            )
        return bool(response["allowed"])
    return status is not None and status not in status_list("late_result_rejected_task_statuses")


def status_list(name: str) -> frozenset[str]:
    values = load_lifecycle_contract().get(name)
    if values is None:
        raise KeyError(f"Unknown Rust lifecycle status list: {name}")
    return frozenset(str(value) for value in values)


def contract_value(name: str) -> str:
    value = load_lifecycle_contract().get(name)
    if value is None:
        raise KeyError(f"Unknown Rust lifecycle contract value: {name}")
    return str(value)


def _load_pyo3_lifecycle_contract(module_name: str) -> dict[str, Any]:
    module = importlib.import_module(module_name)
    contract_json = getattr(module, "lifecycle_contract_json", None)
    if not callable(contract_json):
        raise LifecycleRustContractError("Lifecycle PyO3 module is missing lifecycle_contract_json entrypoint")
    return _parse_json_mapping(contract_json(), "Lifecycle PyO3 contract")


def _pyo3_module_for_enforce_call() -> Any | None:
    if rust_lifecycle_mode() != "enforce":
        return None
    load_lifecycle_contract()
    return importlib.import_module(_pyo3_module_name())


def _pyo3_module_name() -> str:
    return os.environ.get(_MODULE_ENV, "").strip() or _DEFAULT_MODULE_NAME


def _ensure_lifecycle_contract_compatible(actual: Mapping[str, Any], expected: Mapping[str, Any]) -> None:
    for key in ("component", "contract_version", "transition_table_hash", "error_code_table_hash"):
        if actual.get(key) != expected.get(key):
            raise LifecycleRustContractError(
                "Lifecycle PyO3 contract mismatch",
                safe_metadata={"field": key},
            )
    actual_features = {str(feature) for feature in actual.get("supported_features", ())}
    expected_features = {str(feature) for feature in expected.get("supported_features", ())}
    required_features = expected_features | _REQUIRED_FEATURES
    if not required_features.issubset(actual_features):
        raise LifecycleRustContractError(
            "Lifecycle PyO3 contract mismatch",
            safe_metadata={"field": "supported_features"},
        )


def _call_pyo3_json(module: Any, function_name: str, payload: Mapping[str, Any], label: str) -> dict[str, Any]:
    function = getattr(module, function_name, None)
    if not callable(function):
        raise LifecycleRustContractError(
            f"Lifecycle PyO3 module is missing {function_name} entrypoint",
            code="lifecycle_contract_mismatch",
        )
    raw_response = function(json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return _parse_json_mapping(raw_response, label)


def _raise_lifecycle_response_error(response: Mapping[str, Any]) -> None:
    error = response.get("error")
    if error is None:
        return
    if not isinstance(error, Mapping):
        raise LifecycleRustContractError(
            "Lifecycle PyO3 returned malformed error envelope",
            code="lifecycle_structured_output_invalid",
        )
    safe_metadata = error.get("safe_metadata")
    raise LifecycleRustContractError(
        str(error.get("message") or "Lifecycle PyO3 returned an error"),
        code=str(error.get("code") or "lifecycle_structured_output_invalid"),
        category=str(error.get("category") or "lifecycle"),
        retriable=bool(error.get("retriable", False)),
        safe_metadata=safe_metadata if isinstance(safe_metadata, Mapping) else None,
    )


def _parse_json_mapping(raw: Any, label: str) -> dict[str, Any]:
    try:
        parsed = json.loads(str(raw))
    except (TypeError, json.JSONDecodeError) as exc:
        raise LifecycleRustContractError(f"{label} is not valid JSON") from exc
    if not isinstance(parsed, Mapping):
        raise LifecycleRustContractError(f"{label} is not a JSON object")
    return dict(parsed)


load_lifecycle_contract.cache_clear = _load_lifecycle_contract_for_mode.cache_clear  # type: ignore[attr-defined]


__all__ = [
    "can_accept_late_result_status",
    "cancel_node_target",
    "contract_value",
    "load_lifecycle_contract",
    "rust_lifecycle_mode",
    "status_list",
    "transition_allowed",
    "transition_rule",
    "transition_target",
]
