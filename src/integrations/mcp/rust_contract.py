from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

_CONTRACT_PATH = Path(__file__).with_name("rust_contracts") / "mcp_runtime_contract.json"


@lru_cache(maxsize=1)
def load_mcp_runtime_contract() -> dict[str, Any]:
    """Load the Rust-owned MCP runtime contract artifact."""

    with _CONTRACT_PATH.open("r", encoding="utf-8") as handle:
        contract = json.load(handle)
    if contract.get("component") != "maf_mcp_runtime_sidecar":
        raise RuntimeError("MCP runtime contract artifact component mismatch")
    return contract


def status_list(name: str) -> frozenset[str]:
    values = load_mcp_runtime_contract().get(name)
    if values is None:
        raise KeyError(f"Unknown Rust MCP runtime status list: {name}")
    return frozenset(str(value) for value in values)


def contract_value(name: str) -> str:
    value = load_mcp_runtime_contract().get(name)
    if value is None:
        raise KeyError(f"Unknown Rust MCP runtime contract value: {name}")
    return str(value)


__all__ = ["contract_value", "load_mcp_runtime_contract", "status_list"]
