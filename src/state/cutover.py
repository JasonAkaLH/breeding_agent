from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .errors import redact_value


@dataclass(frozen=True, slots=True)
class FreshCutoverInput:
    postgres_dsn: str = field(repr=False)
    schema_ready: bool
    runtime_smoke_ready: bool
    queue_backlog: int
    dead_letter_count: int
    sqlite_history_abandoned: bool
    metadata: Mapping[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True, slots=True)
class FreshCutoverPlan:
    ready: bool
    blockers: tuple[str, ...]
    schema_ready: bool
    runtime_smoke_ready: bool
    queue_backlog: int
    dead_letter_count: int
    sqlite_history_abandoned: bool
    metadata: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def public_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "blockers": self.blockers,
            "schema_ready": self.schema_ready,
            "runtime_smoke_ready": self.runtime_smoke_ready,
            "queue_backlog": self.queue_backlog,
            "dead_letter_count": self.dead_letter_count,
            "sqlite_history_abandoned": self.sqlite_history_abandoned,
            "postgres_dsn": "<configured>",
            "metadata": redact_value(self.metadata),
        }


def build_postgres_fresh_cutover_plan(input_: FreshCutoverInput) -> FreshCutoverPlan:
    blockers: list[str] = []
    if not input_.postgres_dsn.strip():
        blockers.append("postgres_dsn_missing")
    if not input_.schema_ready:
        blockers.append("schema_not_ready")
    if not input_.runtime_smoke_ready:
        blockers.append("runtime_smoke_not_ready")
    if input_.queue_backlog != 0:
        blockers.append("queue_not_drained")
    if input_.dead_letter_count != 0:
        blockers.append("dead_letter_not_empty")
    if not input_.sqlite_history_abandoned:
        blockers.append("sqlite_history_abandonment_not_confirmed")
    return FreshCutoverPlan(
        ready=not blockers,
        blockers=tuple(blockers),
        schema_ready=input_.schema_ready,
        runtime_smoke_ready=input_.runtime_smoke_ready,
        queue_backlog=input_.queue_backlog,
        dead_letter_count=input_.dead_letter_count,
        sqlite_history_abandoned=input_.sqlite_history_abandoned,
        metadata=dict(input_.metadata),
    )


def validate_cutover_report_is_redacted(report: Mapping[str, Any]) -> bool:
    text = repr(report)
    forbidden = ("postgresql://", "postgres://", "password=", "token=", "secret", "biobin@")
    return not any(item in text for item in forbidden)
