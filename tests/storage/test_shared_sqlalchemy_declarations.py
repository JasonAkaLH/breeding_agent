from __future__ import annotations

import ast
import unittest
from pathlib import Path

from src.state.postgres import runtime_schema
from src.storage import sqlalchemy_models
from src.storage.sqlalchemy_base import (
    DateTimeText,
    JSONText,
    NAMING_CONVENTION,
    SQLiteBase,
)
from src.storage.sqlite import base as legacy_base
from src.storage.sqlite import models as legacy_models


class SharedSQLAlchemyDeclarationsTest(unittest.TestCase):
    def test_sqlite_compat_paths_reexport_shared_objects(self) -> None:
        self.assertIs(legacy_base.SQLiteBase, SQLiteBase)
        self.assertIs(legacy_base.JSONText, JSONText)
        self.assertIs(legacy_base.DateTimeText, DateTimeText)
        self.assertIs(legacy_base.NAMING_CONVENTION, NAMING_CONVENTION)

        root = Path(__file__).resolve().parents[2]
        shared_tree = ast.parse(
            (root / "src/storage/sqlalchemy_models.py").read_text(encoding="utf-8")
        )
        row_names = tuple(
            node.name for node in shared_tree.body if isinstance(node, ast.ClassDef)
        )
        self.assertEqual(len(row_names), 60)
        self.assertEqual(tuple(legacy_models.__all__), row_names)
        for name in row_names:
            shared_row = getattr(sqlalchemy_models, name)
            self.assertIs(getattr(legacy_models, name), shared_row, name)
            self.assertIs(shared_row.__table__.metadata, SQLiteBase.metadata, name)
            self.assertEqual(shared_row.__module__, "src.storage.sqlalchemy_models")

    def test_legacy_modules_contain_no_second_declaration_owner(self) -> None:
        root = Path(__file__).resolve().parents[2]
        for relative_path in (
            "src/storage/sqlite/base.py",
            "src/storage/sqlite/models.py",
        ):
            tree = ast.parse((root / relative_path).read_text(encoding="utf-8"))
            self.assertFalse(
                any(isinstance(node, ast.ClassDef) for node in tree.body),
                relative_path,
            )

    def test_postgres_runtime_schema_uses_the_shared_metadata(self) -> None:
        self.assertEqual(len(SQLiteBase.metadata.tables), 60)
        self.assertEqual(
            runtime_schema.POSTGRES_RUNTIME_TABLES,
            tuple(sorted(SQLiteBase.metadata.tables)),
        )


if __name__ == "__main__":
    unittest.main()
