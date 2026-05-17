from __future__ import annotations

import asyncio
import time
import unittest
from types import SimpleNamespace

from src.integrations.mysql_readonly import MySQLReadonlyAdapter, ReadonlyQueryResult, ReadonlyQueryTimeoutError
from src.integrations.rust_safety_contract import DataAccessContractError, resource_limit


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

    def test_default_deadline_comes_from_safety_contract(self) -> None:
        adapter = MySQLReadonlyAdapter(runner=lambda _: ReadonlyQueryResult(columns=(), rows=(), row_count=0))

        self.assertEqual(adapter.deadline_ms, resource_limit("db_deadline_ms"))

    def test_configured_deadline_is_clamped_to_safety_hard_cap(self) -> None:
        adapter = MySQLReadonlyAdapter(
            runner=lambda _: ReadonlyQueryResult(columns=(), rows=(), row_count=0),
            deadline_ms=resource_limit("db_hard_cap_ms") + 1000,
        )

        self.assertEqual(adapter.deadline_ms, resource_limit("db_hard_cap_ms"))

    def test_execution_timeout_uses_stable_data_access_error(self) -> None:
        calls = 0

        def slow_runner(_: str) -> ReadonlyQueryResult:
            nonlocal calls
            calls += 1
            time.sleep(0.05)
            return ReadonlyQueryResult(columns=("id",), rows=({"id": 1},), row_count=1)

        adapter = MySQLReadonlyAdapter(runner=slow_runner, deadline_ms=1)

        with self.assertRaises(ReadonlyQueryTimeoutError) as context:
            asyncio.run(adapter.execute_readonly("SELECT 1", guard_pass_token="guard:test"))

        self.assertEqual(context.exception.code, "data_access_deadline_exceeded")
        self.assertEqual(calls, 1)

    def test_result_shape_errors_preserve_stable_data_access_codes(self) -> None:
        wide_columns = tuple(f"c{index}" for index in range(resource_limit("db_column_limit") + 1))
        cases = [
            (
                "data_access_row_limit_exceeded",
                ReadonlyQueryResult(
                    columns=("id",),
                    rows=tuple({"id": index} for index in range(resource_limit("db_row_limit") + 1)),
                    row_count=resource_limit("db_row_limit") + 1,
                ),
            ),
            (
                "data_access_column_limit_exceeded",
                ReadonlyQueryResult(
                    columns=wide_columns,
                    rows=({column: 1 for column in wide_columns},),
                    row_count=1,
                ),
            ),
            (
                "data_access_result_too_large",
                ReadonlyQueryResult(
                    columns=("payload",),
                    rows=({"payload": "x" * (resource_limit("db_result_bytes") + 1)},),
                    row_count=1,
                ),
            ),
        ]
        for expected_code, query_result in cases:
            with self.subTest(expected_code=expected_code):
                adapter = MySQLReadonlyAdapter(runner=lambda _sql, result=query_result: result)

                with self.assertRaises(DataAccessContractError) as context:
                    asyncio.run(adapter.execute_readonly("SELECT id FROM users", guard_pass_token="guard:test"))

                self.assertEqual(context.exception.code, expected_code)
                self.assertFalse(context.exception.retriable)

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
