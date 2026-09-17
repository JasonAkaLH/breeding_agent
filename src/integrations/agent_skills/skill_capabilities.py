from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping

from src.orchestration.models import CapabilityDescriptor

from .catalog import SkillCatalog
from .manifest import SkillManifest

_CAPABILITY_ID_RE = re.compile(r"^skill\.[a-z0-9_.-]+$")


@dataclass(slots=True, frozen=True)
class SkillCapabilityDiagnostic:
    skill_name: str
    reason: str
    message: str
    source_path_summary: str = ""


@dataclass(slots=True, frozen=True)
class SkillCapabilityRegistry:
    descriptors_by_id: Mapping[str, CapabilityDescriptor]
    skill_name_by_capability_id: Mapping[str, str]
    source_path_by_capability_id: Mapping[str, str] = field(default_factory=dict)
    diagnostics: tuple[SkillCapabilityDiagnostic, ...] = ()

    @property
    def descriptors(self) -> tuple[CapabilityDescriptor, ...]:
        return tuple(self.descriptors_by_id.values())


def build_skill_capability_registry(
    catalog: SkillCatalog,
    *,
    public_skill_roots: Iterable[str | Path],
    reserved_capability_ids: Iterable[str] = (),
) -> SkillCapabilityRegistry:
    roots = tuple(_resolve_root(root) for root in public_skill_roots)
    reserved = set(reserved_capability_ids)
    candidates: dict[str, list[SkillManifest]] = {}
    diagnostics: list[SkillCapabilityDiagnostic] = []

    for skill in catalog.skills:
        if not _is_under_public_root(skill.source_path, roots):
            diagnostics.append(_diagnostic(skill, "not_public_scope", "Skill source is outside public skill roots.", roots=roots))
            continue
        if skill.contract is None:
            if skill.contract_diagnostics:
                for diagnostic in skill.contract_diagnostics:
                    diagnostics.append(
                        _diagnostic(
                            skill,
                            diagnostic.reason,
                            diagnostic.message,
                            roots=roots,
                        )
                    )
            else:
                diagnostics.append(_diagnostic(skill, "contract_missing", "Skill bundle does not contain skill.contract.yaml.", roots=roots))
            continue
        capability_id = skill.contract.capability.id
        if not _CAPABILITY_ID_RE.fullmatch(capability_id) or capability_id in reserved:
            diagnostics.append(
                _diagnostic(skill, "invalid_id", f"Invalid or reserved skill capability id: {capability_id}", roots=roots)
            )
            continue
        candidates.setdefault(capability_id, []).append(skill)

    descriptors: dict[str, CapabilityDescriptor] = {}
    skill_names: dict[str, str] = {}
    source_paths: dict[str, str] = {}
    for capability_id, skills in sorted(candidates.items()):
        if len(skills) > 1:
            for skill in skills:
                diagnostics.append(_diagnostic(skill, "duplicate", f"Duplicate skill capability id: {capability_id}", roots=roots))
            continue
        skill = skills[0]
        assert skill.contract is not None
        descriptor = CapabilityDescriptor(
            capability_id=capability_id,
            name=skill.name,
            display_name=skill.contract.capability.display_name,
            description=_descriptor_description(skill),
            version=skill.contract.capability.version,
            enabled=True,
            public=True,
            kind="skill",
            source="skill",
            source_path=_source_path_summary(skill, roots),
        )
        descriptors[capability_id] = descriptor
        skill_names[capability_id] = skill.name
        source_paths[capability_id] = descriptor.source_path

    return SkillCapabilityRegistry(
        descriptors_by_id=descriptors,
        skill_name_by_capability_id=skill_names,
        source_path_by_capability_id=source_paths,
        diagnostics=tuple(diagnostics),
    )


def _resolve_root(root: str | Path) -> Path:
    try:
        return Path(root).expanduser().resolve()
    except OSError:
        return Path(root).expanduser().absolute()


def _is_under_public_root(path: Path, roots: tuple[Path, ...]) -> bool:
    if not roots:
        return False
    try:
        source = path.expanduser().resolve()
    except OSError:
        source = path.expanduser().absolute()
    for root in roots:
        try:
            source.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _descriptor_description(skill: SkillManifest) -> str:
    assert skill.contract is not None
    description = (skill.contract.capability.description or skill.description or f"Skill: {skill.name}").strip()
    if len(description) > 240:
        return description[:237] + "..."
    return description


def _diagnostic(skill: SkillManifest, reason: str, message: str, *, roots: tuple[Path, ...]) -> SkillCapabilityDiagnostic:
    return SkillCapabilityDiagnostic(
        skill_name=skill.name,
        reason=reason,
        message=message,
        source_path_summary=_source_path_summary(skill, roots),
    )


def _source_path_summary(skill: SkillManifest, roots: tuple[Path, ...]) -> str:
    try:
        source = skill.source_path.expanduser().resolve()
    except OSError:
        source = skill.source_path.expanduser().absolute()
    for root in roots:
        try:
            return source.relative_to(root).as_posix()
        except ValueError:
            continue
    return "<outside-public-roots>"
