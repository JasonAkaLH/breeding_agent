from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

_CONTRACT_PATH = Path(__file__).with_name("rust_contracts") / "runtime_sidecar_contract.json"


@lru_cache(maxsize=1)
def load_runtime_sidecar_contract() -> dict[str, Any]:
    """Load the Rust-owned runtime store/event/dispatcher sidecar contract."""

    with _CONTRACT_PATH.open("r", encoding="utf-8") as handle:
        contract = json.load(handle)
    if contract.get("component") != "maf_runtime_sidecar":
        raise RuntimeError("runtime sidecar contract artifact component mismatch")
    return contract


def migration_policy() -> dict[str, Any]:
    return dict(load_runtime_sidecar_contract()["migration_policy"])


def artifact_policy() -> dict[str, Any]:
    return dict(load_runtime_sidecar_contract()["artifact_policy"])


def benchmark_policy() -> dict[str, Any]:
    return dict(load_runtime_sidecar_contract()["benchmark_policy"])


def config_policy() -> dict[str, Any]:
    return dict(load_runtime_sidecar_contract()["config_policy"])


def decommission_policy() -> dict[str, Any]:
    return dict(load_runtime_sidecar_contract()["decommission_policy"])


def operation_policy(name: str) -> dict[str, Any]:
    for operation in load_runtime_sidecar_contract()["operations"]:
        if operation.get("name") == name:
            return dict(operation)
    raise KeyError(f"Unknown Rust runtime sidecar operation: {name}")


def ops_policy() -> dict[str, Any]:
    return dict(load_runtime_sidecar_contract()["ops_policy"])


def promotion_policy() -> dict[str, Any]:
    return dict(load_runtime_sidecar_contract()["promotion_policy"])


def error_policy(code: str) -> dict[str, Any]:
    for error in load_runtime_sidecar_contract()["error_codes"]:
        if error.get("code") == code:
            return dict(error)
    raise KeyError(f"Unknown Rust runtime sidecar error code: {code}")


def mode_for_component(component: str) -> str:
    contract = load_runtime_sidecar_contract()
    env_name = contract["mode_env"].get(component)
    if env_name is None:
        raise KeyError(f"Unknown Rust runtime sidecar component: {component}")
    mode = os.environ.get(env_name, "off").strip().lower() or "off"
    if mode not in contract["modes"]:
        raise RuntimeError(f"Invalid Rust runtime sidecar mode for {component}: {mode}")
    return mode


def resource_limit(name: str) -> int:
    value = load_runtime_sidecar_contract()["resource_limits"].get(name)
    if value is None:
        raise KeyError(f"Unknown Rust runtime sidecar resource limit: {name}")
    return int(value)


def retry_policy() -> dict[str, Any]:
    return dict(load_runtime_sidecar_contract()["retry_policy"])


__all__ = [
    "artifact_policy",
    "benchmark_policy",
    "config_policy",
    "decommission_policy",
    "error_policy",
    "load_runtime_sidecar_contract",
    "migration_policy",
    "mode_for_component",
    "operation_policy",
    "ops_policy",
    "promotion_policy",
    "retry_policy",
    "resource_limit",
]
