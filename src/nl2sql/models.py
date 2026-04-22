from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence


HintPayload = Mapping[str, Any] | Sequence[Any] | str | None


@dataclass(slots=True, frozen=True)
class SchemaContextRequest:
    route_id: str
    schema_profile_id: str
    user_question: str
    hints: HintPayload = None
    max_tables: int = 4
    max_columns_per_table: int = 8


@dataclass(slots=True, frozen=True)
class FailureDetail:
    code: str
    message: str
    retriable: bool = True
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class JoinHint:
    left_table: str
    left_column: str
    right_table: str
    right_column: str
    reason: str

    @property
    def expression(self) -> str:
        return f"{self.left_table}.{self.left_column} = {self.right_table}.{self.right_column}"


@dataclass(slots=True, frozen=True)
class TableSelection:
    table_name: str
    description: str
    selected_columns: tuple[str, ...]
    reasons: tuple[str, ...]
    score: int


@dataclass(slots=True, frozen=True)
class SchemaContextResult:
    ok: bool
    route_id: str
    schema_profile_id: str
    selected_tables: tuple[str, ...]
    selected_columns: Mapping[str, tuple[str, ...]]
    join_hints: tuple[JoinHint, ...]
    context_summary: str
    table_selections: tuple[TableSelection, ...] = ()
    failure: FailureDetail | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
