from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

_CONTRACT_PATH = Path(__file__).with_name("rust_contracts") / "skill_runtime_contract.json"


@lru_cache(maxsize=1)
def load_skill_runtime_contract() -> dict[str, Any]:
    """Load the Rust-owned generic Skill Runtime policy/sandbox contract."""

    with _CONTRACT_PATH.open("r", encoding="utf-8") as handle:
        contract = json.load(handle)
    if contract.get("component") != "maf_skill_runtime":
        raise RuntimeError("skill runtime contract artifact component mismatch")
    return contract


def status_list(name: str) -> frozenset[str]:
    values = load_skill_runtime_contract().get(name)
    if values is None:
        raise KeyError(f"Unknown Rust Skill Runtime status list: {name}")
    return frozenset(str(value) for value in values)


def contract_mapping(name: str) -> dict[str, str]:
    values = load_skill_runtime_contract().get(name)
    if not isinstance(values, dict):
        raise KeyError(f"Unknown Rust Skill Runtime mapping: {name}")
    return {str(key): str(value) for key, value in values.items()}


def artifact_policy() -> dict[str, Any]:
    return dict(load_skill_runtime_contract()["artifact_policy"])


def benchmark_policy() -> dict[str, Any]:
    return dict(load_skill_runtime_contract()["benchmark_policy"])


def decommission_policy() -> dict[str, Any]:
    return dict(load_skill_runtime_contract()["decommission_policy"])


def error_policy(code: str) -> dict[str, Any]:
    for error in load_skill_runtime_contract()["error_codes"]:
        if error.get("code") == code:
            return dict(error)
    raise KeyError(f"Unknown Rust Skill Runtime error code: {code}")


def ops_policy() -> dict[str, Any]:
    return dict(load_skill_runtime_contract()["ops_policy"])


def promotion_policy() -> dict[str, Any]:
    return dict(load_skill_runtime_contract()["promotion_policy"])


__all__ = [
    "artifact_policy",
    "benchmark_policy",
    "contract_mapping",
    "decommission_policy",
    "error_policy",
    "load_skill_runtime_contract",
    "ops_policy",
    "promotion_policy",
    "status_list",
]
