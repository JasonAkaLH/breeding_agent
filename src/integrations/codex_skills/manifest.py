from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .io_contract import SkillIOContract
from .script_manifest import SkillScriptEntrypoint


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
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def root_dir(self) -> Path:
        return self.source_path.parent
