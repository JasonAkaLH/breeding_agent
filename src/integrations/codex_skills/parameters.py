from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(slots=True, frozen=True)
class SkillParameterSpec:
    name: str
    type: str = "string"
    required: bool = False
    sources: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    patterns: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, name: str, value: Any) -> "SkillParameterSpec":
        if not isinstance(value, Mapping):
            value = {}
        return cls(
            name=name,
            type=str(value.get("type") or "string").strip().lower() or "string",
            required=bool(value.get("required", False)),
            sources=_string_tuple(value.get("sources") or value.get("source")),
            aliases=_string_tuple(value.get("aliases") or value.get("alias")),
            patterns=_string_tuple(value.get("patterns") or value.get("pattern")),
        )


def parse_parameter_specs(value: Any) -> dict[str, SkillParameterSpec]:
    if not isinstance(value, Mapping):
        return {}
    specs: dict[str, SkillParameterSpec] = {}
    for raw_name, raw_spec in value.items():
        name = str(raw_name).strip()
        if not name:
            continue
        specs[name] = SkillParameterSpec.from_mapping(name, raw_spec)
    return specs


def _string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list | tuple):
        return tuple(str(item) for item in value if str(item).strip())
    return ()
