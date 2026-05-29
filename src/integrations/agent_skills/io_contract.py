from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(slots=True, frozen=True)
class SkillIOContract:
    required: tuple[str, ...] = ()
    schema: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, value: Any) -> "SkillIOContract":
        if not isinstance(value, Mapping):
            return cls()
        required_value = value.get("required", ())
        if isinstance(required_value, str):
            required = (required_value,)
        elif isinstance(required_value, list | tuple):
            required = tuple(str(item) for item in required_value)
        else:
            required = ()
        return cls(required=required, schema=dict(value))

    def validate_required(self, payload: Mapping[str, Any]) -> None:
        missing = [key for key in self.required if key not in payload]
        if missing:
            raise ValueError(f"Missing required skill IO fields: {', '.join(missing)}")
