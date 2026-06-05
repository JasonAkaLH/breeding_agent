from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .io_contract import SkillIOContract
from .parameters import SkillParameterSpec
from .script_manifest import SkillScriptEntrypoint
from .contract import SkillContract, SkillContractDiagnostic


@dataclass(slots=True, frozen=True)
class SkillManifest:
    name: str
    description: str
    triggers: tuple[str, ...]
    body: str
    source_path: Path
    inputs: SkillIOContract = SkillIOContract()
    outputs: SkillIOContract = SkillIOContract()
    scripts: tuple[SkillScriptEntrypoint, ...] = ()
    parameters: Mapping[str, SkillParameterSpec] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    contract: SkillContract | None = None
    contract_diagnostics: tuple[SkillContractDiagnostic, ...] = ()

    @property
    def root_dir(self) -> Path:
        return self.source_path.parent
