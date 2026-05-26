from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .errors import redact_value

MIGRATION_OBJECTS = (
    "conversation",
    "message",
    "task",
    "task_node",
    "task_edge",
    "event",
    "artifact",
    "interrupt",
    "cancellation",
    "mailbox",
    "pending_skill_context",
    "conversation_memory_summary",
    "auth_user_token",
)


@dataclass(frozen=True, slots=True)
class SQLiteToPostgresMigrationPlan:
    sqlite_path: Path
    postgres_dsn: str = field(repr=False)
    dry_run: bool
    writes_enabled: bool
    objects: tuple[str, ...]
    checkpoints_enabled: bool = True
    operator_confirmation_required: bool = True

    def public_dict(self) -> dict[str, Any]:
        return {
            "sqlite_path": str(self.sqlite_path),
            "postgres_dsn": "<configured>",
            "dry_run": self.dry_run,
            "writes_enabled": self.writes_enabled,
            "objects": self.objects,
            "checkpoints_enabled": self.checkpoints_enabled,
            "operator_confirmation_required": self.operator_confirmation_required,
        }


def build_sqlite_to_postgres_migration_plan(
    *,
    sqlite_path: Path,
    postgres_dsn: str,
    dry_run: bool,
    operator_confirmation: bool = False,
) -> SQLiteToPostgresMigrationPlan:
    if not sqlite_path.exists():
        raise FileNotFoundError(sqlite_path)
    if not dry_run and not operator_confirmation:
        raise ValueError("non-dry-run migration requires operator confirmation")
    if not postgres_dsn.strip():
        raise ValueError("postgres_dsn is required and must come from env/git-ignored config")
    return SQLiteToPostgresMigrationPlan(
        sqlite_path=sqlite_path,
        postgres_dsn=postgres_dsn,
        dry_run=dry_run,
        writes_enabled=not dry_run,
        objects=MIGRATION_OBJECTS,
    )


def validate_migration_report_is_redacted(report: Mapping[str, Any]) -> bool:
    text = repr(report)
    forbidden = ("postgresql://", "postgres://", "password=", "token=", "u:p", "user:pass")
    return not any(item in text for item in forbidden)


@dataclass(frozen=True, slots=True)
class CutoverReadiness:
    ready: bool
    blockers: tuple[str, ...] = ()


def evaluate_cutover_readiness(
    *,
    dry_run_passed: bool,
    validation_passed: bool,
    shadow_compare_passed: bool,
    queue_backlog: int,
    dead_letter_count: int,
) -> CutoverReadiness:
    blockers: list[str] = []
    if not dry_run_passed:
        blockers.append("dry_run_not_passed")
    if not validation_passed:
        blockers.append("validation_not_passed")
    if not shadow_compare_passed:
        blockers.append("shadow_compare_not_passed")
    if queue_backlog != 0:
        blockers.append("queue_not_drained")
    if dead_letter_count != 0:
        blockers.append("dead_letter_not_empty")
    return CutoverReadiness(ready=not blockers, blockers=tuple(blockers))


@dataclass(frozen=True, slots=True)
class MigrationEvidence:
    migration_id: str
    status: str
    row_counts: Mapping[str, int] = field(default_factory=dict)
    checksums: Mapping[str, str] = field(default_factory=dict)
    pending_gates: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def public_dict(self) -> dict[str, Any]:
        return {
            "migration_id": self.migration_id,
            "status": self.status,
            "row_counts": dict(self.row_counts),
            "checksums": dict(self.checksums),
            "pending_gates": self.pending_gates,
            "metadata": _redact_metadata(self.metadata),
        }


def _redact_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    return redact_value(metadata)
