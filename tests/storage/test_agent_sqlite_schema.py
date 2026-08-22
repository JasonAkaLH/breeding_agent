from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sqlalchemy import inspect

from src.storage.sqlite import bootstrap_sqlite_database, create_sqlite_engine


class AgentSQLiteSchemaTest(unittest.TestCase):
    def test_agent_tables_are_additive_and_old_task_tables_remain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = create_sqlite_engine(Path(directory) / "schema.sqlite3")
            try:
                bootstrap_sqlite_database(engine)
                tables = set(inspect(engine).get_table_names())
            finally:
                engine.dispose()
        self.assertTrue({"agent_run", "agent_item", "agent_final_receipt"}.issubset(tables))
        self.assertTrue({"task", "task_node", "task_edge"}.issubset(tables))
