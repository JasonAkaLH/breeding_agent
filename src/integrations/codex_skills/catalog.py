from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import yaml

from .manifest import SkillManifest
from .parser import SkillParseError, parse_skill_file


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
                    manifests.append(parse_skill_file(skill_file))
                except (OSError, SkillParseError, yaml.YAMLError, ValueError):
                    continue
        return cls(tuple(manifests))

    def get(self, name: str) -> SkillManifest | None:
        normalized = name.strip().lower()
        for skill in self.skills:
            if skill.name.lower() == normalized:
                return skill
        return None
