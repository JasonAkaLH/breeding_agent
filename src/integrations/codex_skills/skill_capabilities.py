from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping

from src.orchestration.models import CapabilityDescriptor

from .catalog import SkillCatalog
from .manifest import SkillManifest

_CAPABILITY_ID_RE = re.compile(r"^skill\.[a-z0-9_.-]+$")
_SUPPORTED_PUBLIC_SCRIPT_RUNTIMES = frozenset({"python"})


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
            diagnostics.append(
                _diagnostic(
                    skill,
                    "not_public_scope",
                    "Skill source is outside public skill roots.",
                    roots=roots,
                )
            )
            continue
        unsupported_runtime = _unsupported_runtime(skill)
        if unsupported_runtime:
            diagnostics.append(
                _diagnostic(
                    skill,
                    "unsupported_runtime",
                    f"Unsupported public skill script runtime: {unsupported_runtime}",
                    roots=roots,
                )
            )
            continue
        capability_id = _manifest_capability_id(skill) or _derive_capability_id(skill.name)
        if not _CAPABILITY_ID_RE.fullmatch(capability_id) or capability_id in reserved:
            diagnostics.append(
                _diagnostic(
                    skill,
                    "invalid_id",
                    f"Invalid or reserved skill capability id: {capability_id}",
                    roots=roots,
                )
            )
            continue
        candidates.setdefault(capability_id, []).append(skill)

    descriptors: dict[str, CapabilityDescriptor] = {}
    skill_names: dict[str, str] = {}
    source_paths: dict[str, str] = {}
    for capability_id, skills in sorted(candidates.items()):
        if len(skills) > 1:
            for skill in skills:
                diagnostics.append(
                    _diagnostic(
                        skill,
                        "duplicate",
                        f"Duplicate skill capability id: {capability_id}",
                        roots=roots,
                    )
                )
            continue
        skill = skills[0]
        descriptors[capability_id] = CapabilityDescriptor(
            capability_id=capability_id,
            name=skill.name,
            description=_descriptor_description(skill),
            version=str(_metadata_value(skill, "version") or "1"),
            enabled=True,
            public=True,
            kind="skill",
            source="skill",
            source_path=_source_path_summary(skill, roots),
        )
        skill_names[capability_id] = skill.name
        source_paths[capability_id] = descriptors[capability_id].source_path

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


def _manifest_capability_id(skill: SkillManifest) -> str | None:
    direct = _metadata_value(skill, "capability_id")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    nested = skill.metadata.get("metadata")
    if isinstance(nested, Mapping):
        nested_value = nested.get("capability_id")
        if isinstance(nested_value, str) and nested_value.strip():
            return nested_value.strip()
    return None


def _metadata_value(skill: SkillManifest, key: str):
    value = skill.metadata.get(key)
    if value not in (None, ""):
        return value
    nested = skill.metadata.get("metadata")
    if isinstance(nested, Mapping):
        return nested.get(key)
    return None


def _derive_capability_id(name: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    normalized = re.sub(r"_+", "_", normalized)
    return f"skill.{normalized}" if normalized else "skill.unnamed"


def _descriptor_description(skill: SkillManifest) -> str:
    description = skill.description.strip() or f"Skill: {skill.name}"
    if len(description) > 240:
        return description[:237] + "..."
    return description


def _unsupported_runtime(skill: SkillManifest) -> str:
    for script in skill.scripts:
        runtime = script.runtime.strip().lower()
        if runtime not in _SUPPORTED_PUBLIC_SCRIPT_RUNTIMES:
            return runtime or "<empty>"
    return ""


def _diagnostic(
    skill: SkillManifest,
    reason: str,
    message: str,
    *,
    roots: tuple[Path, ...],
) -> SkillCapabilityDiagnostic:
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
