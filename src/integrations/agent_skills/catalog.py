from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable

import yaml

from .manifest import SkillManifest
from .parser import SkillParseError, parse_skill_file
from .contract import (
    SkillContractDiagnostic,
    SkillContractParseError,
    forbidden_v1_fields,
    load_skill_frontmatter,
    parse_skill_contract_file,
)


@dataclass(slots=True, frozen=True)
class SkillCatalog:
    skills: tuple[SkillManifest, ...]

    @classmethod
    def from_roots(cls, roots: Iterable[str | Path]) -> "SkillCatalog":
        manifests: list[SkillManifest] = []
        for root in roots:
            root_path = Path(root).expanduser()
            if not root_path.exists():
                continue
            for skill_file in sorted(root_path.rglob("SKILL.md")):
                try:
                    manifest = parse_skill_file(skill_file)
                    manifests.append(_attach_contract(manifest))
                except (OSError, SkillParseError, yaml.YAMLError, ValueError):
                    continue
        return cls(tuple(manifests))

    def get(self, name: str) -> SkillManifest | None:
        normalized = name.strip().lower()
        for skill in self.skills:
            if skill.name.lower() == normalized:
                return skill
        return None


def _attach_contract(manifest: SkillManifest) -> SkillManifest:
    contract_path = manifest.root_dir / "skill.contract.yaml"
    diagnostics: list[SkillContractDiagnostic] = []
    if not contract_path.exists():
        diagnostics.append(
            SkillContractDiagnostic(
                skill_name=manifest.name,
                reason="contract_missing",
                message="Skill bundle does not contain skill.contract.yaml.",
                source_path_summary=str(manifest.source_path),
            )
        )
        return replace(manifest, contract=None, contract_diagnostics=tuple(diagnostics))

    forbidden = forbidden_v1_fields(load_skill_frontmatter(manifest.source_path))
    if forbidden:
        diagnostics.append(
            SkillContractDiagnostic(
                skill_name=manifest.name,
                reason="v1_field_forbidden",
                message=f"V2 Skill frontmatter contains forbidden v1 platform fields: {', '.join(forbidden)}.",
                source_path_summary=str(manifest.source_path),
            )
        )
        return replace(manifest, contract=None, contract_diagnostics=tuple(diagnostics))

    try:
        contract = parse_skill_contract_file(contract_path)
    except (OSError, SkillContractParseError, yaml.YAMLError, ValueError) as exc:
        diagnostics.append(
            SkillContractDiagnostic(
                skill_name=manifest.name,
                reason="contract_invalid",
                message=str(exc),
                source_path_summary=str(contract_path),
            )
        )
        return replace(manifest, contract=None, contract_diagnostics=tuple(diagnostics))
    return replace(manifest, contract=contract, contract_diagnostics=())
