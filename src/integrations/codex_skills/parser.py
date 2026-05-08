from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import yaml

from .io_contract import SkillIOContract
from .manifest import SkillManifest
from .parameters import parse_parameter_specs
from .script_manifest import SkillScriptEntrypoint


class SkillParseError(ValueError):
    pass


_FRONTMATTER = "---"
_KNOWN_FIELDS = {"name", "description", "triggers", "inputs", "outputs", "scripts", "parameters", "input_parameters"}


def parse_skill_file(path: str | Path) -> SkillManifest:
    source_path = Path(path)
    text = source_path.read_text(encoding="utf-8")
    frontmatter, body = _split_frontmatter(text, source_path)
    data = yaml.safe_load(frontmatter) or {}
    if not isinstance(data, Mapping):
        raise SkillParseError(f"Skill frontmatter must be a mapping: {source_path}")

    name = str(data.get("name") or "").strip()
    description = str(data.get("description") or "").strip()
    body = body.strip()
    if not name:
        raise SkillParseError(f"Skill name is required: {source_path}")
    if not body:
        raise SkillParseError(f"Skill body is required: {source_path}")

    return SkillManifest(
        name=name,
        description=description,
        triggers=_string_tuple(data.get("triggers")),
        body=body,
        source_path=source_path,
        inputs=SkillIOContract.from_mapping(data.get("inputs")),
        outputs=SkillIOContract.from_mapping(data.get("outputs")),
        scripts=_parse_scripts(data.get("scripts"), source_path),
        parameters=parse_parameter_specs(data.get("parameters") or data.get("input_parameters")),
        metadata={key: value for key, value in data.items() if key not in _KNOWN_FIELDS},
    )


def _split_frontmatter(text: str, source_path: Path) -> tuple[str, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != _FRONTMATTER:
        raise SkillParseError(f"Skill file must start with frontmatter: {source_path}")
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == _FRONTMATTER:
            return "\n".join(lines[1:index]), "\n".join(lines[index + 1 :])
    raise SkillParseError(f"Skill frontmatter is not closed: {source_path}")


def _string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list | tuple):
        return tuple(str(item) for item in value if str(item).strip())
    return ()


def _parse_scripts(value: Any, source_path: Path) -> tuple[SkillScriptEntrypoint, ...]:
    if value is None:
        return ()
    entries = value if isinstance(value, list | tuple) else [value]
    scripts: list[SkillScriptEntrypoint] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise SkillParseError(f"Skill script entry must be a mapping: {source_path}")
        script = SkillScriptEntrypoint.from_mapping(entry)
        if not script.name or not script.path:
            raise SkillParseError(f"Skill script entry requires name and path: {source_path}")
        scripts.append(script)
    return tuple(scripts)
