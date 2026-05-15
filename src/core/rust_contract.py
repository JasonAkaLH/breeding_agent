from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

_CONTRACT_PATH = Path(__file__).with_name("rust_contracts") / "core_contract.json"


@lru_cache(maxsize=1)
def load_core_contract() -> dict[str, Any]:
    """Load the Rust-owned core contract artifact.

    `maf_core_types` is the canonical source for enum values, model field
    snapshots, and stable core error-code metadata. Python keeps this tiny
    loader so existing import paths can remain stable without re-defining the
    contract in Python.
    """

    with _CONTRACT_PATH.open("r", encoding="utf-8") as handle:
        contract = json.load(handle)
    if contract.get("component") != "maf_core_types":
        raise RuntimeError("core contract artifact component mismatch")
    return contract


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


__all__ = ["core_enum_members", "core_model_fields", "load_core_contract"]
