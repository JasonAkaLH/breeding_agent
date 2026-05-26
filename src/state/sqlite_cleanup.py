from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class SQLiteCleanupConfirmationError(RuntimeError):
    pass


class SQLiteCleanupScopeError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SQLiteCleanupResult:
    deleted_count: int
    archived_count: int
    skipped_count: int


@dataclass(frozen=True, slots=True)
class SQLiteCleanupPlan:
    runtime_dir: Path
    candidates: tuple[Path, ...]
    dry_run: bool = True
    confirm: bool = False
    archive: bool = True

    @property
    def action(self) -> str:
        if self.dry_run:
            return "dry_run"
        return "archive" if self.archive else "delete"

    def public_dict(self) -> dict[str, object]:
        return {
            "runtime_dir": str(self.runtime_dir),
            "candidate_count": len(self.candidates),
            "dry_run": self.dry_run,
            "action": self.action,
        }

    def apply(self) -> SQLiteCleanupResult:
        if self.dry_run:
            return SQLiteCleanupResult(0, 0, len(self.candidates))
        if not self.confirm:
            raise SQLiteCleanupConfirmationError("SQLite cleanup requires explicit operator confirmation")
        deleted = 0
        archived = 0
        skipped = 0
        for candidate in self.candidates:
            if not candidate.exists():
                skipped += 1
                continue
            if self.archive:
                target = candidate.with_name(candidate.name + ".postgresql-fresh-cutover-archive")
                candidate.replace(target)
                archived += 1
            else:
                candidate.unlink()
                deleted += 1
        return SQLiteCleanupResult(deleted, archived, skipped)


def build_sqlite_cleanup_plan(
    *,
    runtime_dir: str | Path,
    candidates: list[str | Path] | tuple[str | Path, ...],
    dry_run: bool = True,
    confirm: bool = False,
    archive: bool = True,
) -> SQLiteCleanupPlan:
    root = Path(runtime_dir).resolve()
    resolved_candidates = tuple(Path(candidate).resolve() for candidate in candidates)
    for candidate in resolved_candidates:
        if not _is_relative_to(candidate, root):
            raise SQLiteCleanupScopeError(f"SQLite cleanup candidate is outside runtime dir: {candidate.name}")
    if not dry_run and not confirm:
        raise SQLiteCleanupConfirmationError("SQLite cleanup requires explicit operator confirmation")
    return SQLiteCleanupPlan(root, resolved_candidates, dry_run=dry_run, confirm=confirm, archive=archive)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
