from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .io_contract import SkillIOContract


@dataclass(slots=True, frozen=True)
class SkillScriptEntrypoint:
    name: str
    path: str
    runtime: str = "python"
    auto_run: bool = False
    timeout_seconds: float = 10.0
    input_contract: SkillIOContract = SkillIOContract()
    output_contract: SkillIOContract = SkillIOContract()

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "SkillScriptEntrypoint":
        name = str(value.get("name") or "").strip()
        path = str(value.get("path") or "").strip()
        runtime = str(value.get("runtime") or "python").strip().lower()
        auto_run = bool(value.get("auto_run") or value.get("run_by_default") or False)
        timeout_raw = value.get("timeout_seconds", 10)
        timeout_seconds = float(timeout_raw)
        return cls(
            name=name,
            path=path,
            runtime=runtime,
            auto_run=auto_run,
            timeout_seconds=timeout_seconds,
            input_contract=SkillIOContract.from_mapping(value.get("inputs") or value.get("input")),
            output_contract=SkillIOContract.from_mapping(value.get("outputs") or value.get("output")),
        )
