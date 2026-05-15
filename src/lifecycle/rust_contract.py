from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

_CONTRACT_PATH = Path(__file__).with_name("rust_contracts") / "lifecycle_contract.json"


@lru_cache(maxsize=1)
def load_lifecycle_contract() -> dict[str, Any]:
    """Load the Rust-owned lifecycle transition contract artifact."""

    with _CONTRACT_PATH.open("r", encoding="utf-8") as handle:
        contract = json.load(handle)
    if contract.get("component") != "maf_lifecycle":
        raise RuntimeError("lifecycle contract artifact component mismatch")
    return contract


def transition_rule(operation: str) -> dict[str, Any]:
    rule = load_lifecycle_contract()["transitions"].get(operation)
    if rule is None:
        raise KeyError(f"Unknown Rust lifecycle transition: {operation}")
    return rule


def transition_allowed(operation: str, current_status: object) -> bool:
    current = str(current_status)
    return current in transition_rule(operation)["from"]


def transition_target(operation: str) -> str:
    return str(transition_rule(operation)["to"])


def cancel_node_target(current_status: object) -> str | None:
    return load_lifecycle_contract()["cancel_node_targets"].get(str(current_status))


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


__all__ = [
    "cancel_node_target",
    "contract_value",
    "load_lifecycle_contract",
    "status_list",
    "transition_allowed",
    "transition_rule",
    "transition_target",
]
