from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from src.integrations.mysql_readonly import MySQLReadonlyAdapter, ReadonlyQueryResult


class _FakeResult:
    def __init__(self) -> None:
        self._rows = [SimpleNamespace(_mapping={"variety_name": "龙粳33"})]

    def __iter__(self):
        return iter(self._rows)

    def keys(self):
        return ("variety_name",)


class _FakeConnection:
    def __init__(self, engine: "_FakeEngine") -> None:
        self._engine = engine

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def execute(self, statement):
        self._engine.executed_sql.append(str(statement))
        return _FakeResult()


class _FakeEngine:
    def __init__(self) -> None:
        self.connect_count = 0
        self.dispose_count = 0
        self.executed_sql: list[str] = []

    def connect(self) -> _FakeConnection:
        self.connect_count += 1
        return _FakeConnection(self)

    def dispose(self) -> None:
        self.dispose_count += 1


class MySQLReadonlyAdapterTest(unittest.TestCase):
    def test_requires_guard_pass_token(self) -> None:
        adapter = MySQLReadonlyAdapter(runner=lambda _: ReadonlyQueryResult(columns=(), rows=(), row_count=0))

        with self.assertRaises(PermissionError):
            asyncio.run(adapter.execute_readonly("SELECT 1", guard_pass_token=None))

    def test_runner_path_keeps_test_injection(self) -> None:
        adapter = MySQLReadonlyAdapter(
            runner=lambda sql: ReadonlyQueryResult(
                columns=("sql",),
                rows=({"sql": sql},),
                row_count=1,
            )
        )

        result = asyncio.run(adapter.execute_readonly("SELECT 1", guard_pass_token="guard:test"))

        self.assertEqual(result.columns, ("sql",))
        self.assertEqual(result.rows, ({"sql": "SELECT 1"},))

    def test_engine_factory_is_lazy_reused_and_disposable(self) -> None:
        engine = _FakeEngine()
        calls = 0

        def engine_factory():
            nonlocal calls
            calls += 1
            return engine

        adapter = MySQLReadonlyAdapter(engine_factory=engine_factory)

        first = asyncio.run(adapter.execute_readonly("SELECT variety_name FROM variety LIMIT 1", guard_pass_token="guard:test"))
        second = asyncio.run(adapter.execute_readonly("SELECT variety_name FROM variety LIMIT 1", guard_pass_token="guard:test"))
        adapter.close()

        self.assertEqual(calls, 1)
        self.assertEqual(engine.connect_count, 2)
        self.assertEqual(engine.dispose_count, 1)
        self.assertEqual(first.rows, ({"variety_name": "龙粳33"},))
        self.assertEqual(second.columns, ("variety_name",))


if __name__ == "__main__":
    unittest.main()
