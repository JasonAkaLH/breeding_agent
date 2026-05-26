from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from src.state.sqlite_cleanup import (
    SQLiteCleanupConfirmationError,
    SQLiteCleanupScopeError,
    build_sqlite_cleanup_plan,
)


class SQLiteCleanupAfterPostgresCutoverTest(unittest.TestCase):
    def test_cleanup_defaults_to_dry_run_and_does_not_delete(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "state.db"
            db.write_text("fixture", encoding="utf-8")
            plan = build_sqlite_cleanup_plan(runtime_dir=root, candidates=[db])
            self.assertTrue(plan.dry_run)
            self.assertTrue(db.exists())
            self.assertEqual(plan.action, "dry_run")

    def test_cleanup_refuses_apply_without_confirmation(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "state.db"
            db.write_text("fixture", encoding="utf-8")
            with self.assertRaises(SQLiteCleanupConfirmationError):
                build_sqlite_cleanup_plan(runtime_dir=root, candidates=[db], dry_run=False, confirm=False)

    def test_cleanup_refuses_paths_outside_runtime_dir(self) -> None:
        with TemporaryDirectory() as tmp, TemporaryDirectory() as outside:
            root = Path(tmp)
            db = Path(outside) / "state.db"
            db.write_text("fixture", encoding="utf-8")
            with self.assertRaises(SQLiteCleanupScopeError):
                build_sqlite_cleanup_plan(runtime_dir=root, candidates=[db])

    def test_cleanup_archive_renames_only_with_confirmation(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "state.db"
            db.write_text("fixture", encoding="utf-8")
            plan = build_sqlite_cleanup_plan(runtime_dir=root, candidates=[db], dry_run=False, confirm=True, archive=True)
            result = plan.apply()
            self.assertFalse(db.exists())
            self.assertEqual(result.deleted_count, 0)
            self.assertEqual(result.archived_count, 1)
            self.assertTrue((root / "state.db.postgresql-fresh-cutover-archive").exists())
