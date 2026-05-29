from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .value_utils import string_tuple


@dataclass(slots=True, frozen=True)
class SkillParameterSpec:
    name: str
    type: str = "string"
    required: bool = False
    sources: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    patterns: tuple[str, ...] = ()
    default: Any | None = None
    enum: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, name: str, value: Any) -> "SkillParameterSpec":
        if not isinstance(value, Mapping):
            value = {}
        return cls(
            name=name,
            type=str(value.get("type") or "string").strip().lower() or "string",
            required=bool(value.get("required", False)),
            sources=string_tuple(value.get("sources") or value.get("source")),
            aliases=string_tuple(value.get("aliases") or value.get("alias")),
            patterns=string_tuple(value.get("patterns") or value.get("pattern")),
            default=value.get("default"),
            enum=string_tuple(value.get("enum") or value.get("choices")),
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
