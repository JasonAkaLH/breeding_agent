from __future__ import annotations

import _bootstrap  # noqa: F401
import asyncio
import time
import unittest

from sql_query_skill.sql_execute_readonly import SQLQuerySQLExecuteReadonlyCapability
from src.integrations.mysql_readonly import MySQLReadonlyAdapter, TransientReadonlyExecutionError

from support import fake_query_result, make_request


class SQLQuerySQLExecuteReadonlyTest(unittest.TestCase):
    def test_missing_guard_token_is_rejected(self) -> None:
        capability = SQLQuerySQLExecuteReadonlyCapability(adapter=MySQLReadonlyAdapter(runner=lambda sql: fake_query_result()))
        request = make_request(
            "skill.sql_query",
            dependency_outputs={"guard": {"sql": "SELECT 1"}},
        )

        result = asyncio.run(capability.execute(request))

        self.assertIsNotNone(result.error)
        self.assertEqual(result.error.code, "guard_token_missing")

    def test_executes_sql_with_guard_token(self) -> None:
        received: list[str] = []

        def runner(sql: str):
            received.append(sql)
            return fake_query_result()

        capability = SQLQuerySQLExecuteReadonlyCapability(adapter=MySQLReadonlyAdapter(runner=runner))
        request = make_request(
            "skill.sql_query",
            dependency_outputs={"guard": {"sql": "SELECT variety_name FROM variety", "guard_pass_token": "guard:ok"}},
        )

        result = asyncio.run(capability.execute(request))

        self.assertIsNone(result.error)
        self.assertEqual(received, ["SELECT variety_name FROM variety"])
        self.assertEqual(result.output_payload["row_count"], 1)
        self.assertEqual(result.output_payload["preview_row_count"], 1)
        self.assertFalse(result.output_payload["truncated"])
        self.assertEqual(result.output_payload["rows"], [{"variety_name": "龙粳33"}])

    def test_transient_error_retries_once(self) -> None:
        calls = {"count": 0}

        def runner(sql: str):
            calls["count"] += 1
            if calls["count"] == 1:
                raise TransientReadonlyExecutionError("temporary")
            return fake_query_result()

        capability = SQLQuerySQLExecuteReadonlyCapability(adapter=MySQLReadonlyAdapter(runner=runner, transient_retries=1))
        request = make_request(
            "skill.sql_query",
            dependency_outputs={"guard": {"sql": "SELECT variety_name FROM variety", "guard_pass_token": "guard:ok"}},
        )

        result = asyncio.run(capability.execute(request))

        self.assertIsNone(result.error)
        self.assertEqual(calls["count"], 2)

    def test_readonly_policy_error_preserves_data_access_code(self) -> None:
        capability = SQLQuerySQLExecuteReadonlyCapability(adapter=MySQLReadonlyAdapter(runner=lambda sql: fake_query_result()))
        request = make_request(
            "skill.sql_query",
            dependency_outputs={"guard": {"sql": "SELECT * FROM users FOR SHARE", "guard_pass_token": "guard:ok"}},
        )

        result = asyncio.run(capability.execute(request))

        self.assertIsNotNone(result.error)
        self.assertEqual(result.error.code, "data_access_write_denied")
        self.assertFalse(result.error.retriable)

    def test_timeout_error_preserves_data_access_code(self) -> None:
        def slow_runner(sql: str):
            time.sleep(0.05)
            return fake_query_result()

        capability = SQLQuerySQLExecuteReadonlyCapability(adapter=MySQLReadonlyAdapter(runner=slow_runner, deadline_ms=1))
        request = make_request(
            "skill.sql_query",
            dependency_outputs={"guard": {"sql": "SELECT variety_name FROM variety", "guard_pass_token": "guard:ok"}},
        )

        result = asyncio.run(capability.execute(request))

        self.assertIsNotNone(result.error)
        self.assertEqual(result.error.code, "data_access_deadline_exceeded")
        self.assertFalse(result.error.retriable)

    def test_result_shape_error_preserves_data_access_code(self) -> None:
        rows = tuple({"id": index} for index in range(501))
        capability = SQLQuerySQLExecuteReadonlyCapability(
            adapter=MySQLReadonlyAdapter(
                runner=lambda _sql: fake_query_result(columns=("id",), rows=rows),
            )
        )
        request = make_request(
            "skill.sql_query",
            dependency_outputs={"guard": {"sql": "SELECT id FROM users", "guard_pass_token": "guard:ok"}},
        )

        result = asyncio.run(capability.execute(request))

        self.assertIsNotNone(result.error)
        self.assertEqual(result.error.code, "data_access_row_limit_exceeded")
        self.assertFalse(result.error.retriable)
