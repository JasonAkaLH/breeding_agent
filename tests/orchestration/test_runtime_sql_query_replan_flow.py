from __future__ import annotations

import ast
from pathlib import Path
import unittest


class RuntimeSQLQueryReplanFlowTest(unittest.TestCase):
    def test_orchestration_layer_no_longer_imports_sql_query_runtime_replanner(self) -> None:
        orchestration_dir = Path("src/orchestration")
        offenders: list[str] = []
        for path in orchestration_dir.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module and "sql_query" in node.module:
                    offenders.append(f"{path}:{node.module}")
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if "sql_query" in alias.name:
                            offenders.append(f"{path}:{alias.name}")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
