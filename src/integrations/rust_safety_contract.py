from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

_CONTRACT_PATH = Path(__file__).with_name("rust_contracts") / "safety_contract.json"


@lru_cache(maxsize=1)
def load_safety_contract() -> dict[str, Any]:
    """Load the Rust-owned Artifact/Auth/DataAccess/Audit safety contract."""

    with _CONTRACT_PATH.open("r", encoding="utf-8") as handle:
        contract = json.load(handle)
    if contract.get("component") != "maf_safety_kernels":
        raise RuntimeError("safety contract artifact component mismatch")
    return contract


def resource_limit(name: str) -> int:
    value = load_safety_contract()["resource_limits"].get(name)
    if value is None:
        raise KeyError(f"Unknown Rust safety resource limit: {name}")
    return int(value)


__all__ = ["load_safety_contract", "resource_limit"]
