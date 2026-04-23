from __future__ import annotations

import asyncio
import unittest

from src.capabilities.nl2sql.sql_execute_readonly import NL2SQLSQLExecuteReadonlyCapability
from src.integrations.mysql_readonly import MySQLReadonlyAdapter, TransientReadonlyExecutionError

from tests.capabilities.nl2sql.support import fake_query_result, make_request


class NL2SQLSQLExecuteReadonlyTest(unittest.TestCase):
    def test_missing_guard_token_is_rejected(self) -> None:
        capability = NL2SQLSQLExecuteReadonlyCapability(adapter=MySQLReadonlyAdapter(runner=lambda sql: fake_query_result()))
        request = make_request(
            "nl2sql.sql_execute_readonly",
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

        capability = NL2SQLSQLExecuteReadonlyCapability(adapter=MySQLReadonlyAdapter(runner=runner))
        request = make_request(
            "nl2sql.sql_execute_readonly",
            dependency_outputs={"guard": {"sql": "SELECT variety_name FROM variety LIMIT 20", "guard_pass_token": "guard:ok"}},
        )

        result = asyncio.run(capability.execute(request))

        self.assertIsNone(result.error)
        self.assertEqual(received, ["SELECT variety_name FROM variety LIMIT 20"])
        self.assertEqual(result.output_payload["row_count"], 1)

    def test_transient_error_retries_once(self) -> None:
        calls = {"count": 0}

        def runner(sql: str):
            calls["count"] += 1
            if calls["count"] == 1:
                raise TransientReadonlyExecutionError("temporary")
            return fake_query_result()

        capability = NL2SQLSQLExecuteReadonlyCapability(adapter=MySQLReadonlyAdapter(runner=runner, transient_retries=1))
        request = make_request(
            "nl2sql.sql_execute_readonly",
            dependency_outputs={"guard": {"sql": "SELECT variety_name FROM variety LIMIT 20", "guard_pass_token": "guard:ok"}},
        )

        result = asyncio.run(capability.execute(request))

        self.assertIsNone(result.error)
        self.assertEqual(calls["count"], 2)
