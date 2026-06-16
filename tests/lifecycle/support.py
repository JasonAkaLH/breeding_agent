from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.storage.sqlite import SQLiteStorage, bootstrap_sqlite_database, create_sqlite_engine, create_sqlite_session_factory


class LifecycleSQLiteTestCase(unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmpdir.name) / "phase3-lifecycle.sqlite3"
        self.engine = create_sqlite_engine(self.db_path)
        self.session_factory = create_sqlite_session_factory(self.engine)
        bootstrap_sqlite_database(self.engine)
        self.storage = SQLiteStorage(self.session_factory)

    def tearDown(self) -> None:
        self.engine.dispose()
        self._tmpdir.cleanup()
        super().tearDown()
