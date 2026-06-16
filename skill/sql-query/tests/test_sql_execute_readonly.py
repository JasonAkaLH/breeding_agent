from __future__ import annotations

import _bootstrap  # noqa: F401
import asyncio
import json
import time
import unittest

from sql_query_skill.sql_execute_readonly import SQLQuerySQLExecuteReadonlyCapability
from src.integrations.mysql_readonly import MySQLReadonlyAdapter, TransientReadonlyExecutionError
from src.integrations.rust_safety_contract import resource_limit

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
        self.assertNotIn("guard_pass_token", result.output_payload)
        artifact_payload = json.loads(result.artifacts[0].storage_ref)
        self.assertNotIn("guard_pass_token", artifact_payload)

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

    def test_row_limit_overflow_keeps_latest_rows_and_trim_metadata(self) -> None:
        row_limit = resource_limit("db_row_limit")
        rows = tuple({"id": index} for index in range(row_limit + 1))
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

        self.assertIsNone(result.error)
        self.assertEqual(result.output_payload["rows"], [{"id": index} for index in range(1, row_limit + 1)])
        self.assertEqual(result.output_payload["row_count"], row_limit)
        self.assertEqual(result.output_payload["source_row_count"], row_limit + 1)
        self.assertTrue(result.output_payload["row_limit_trimmed"])
        self.assertTrue(result.output_payload["truncated"])
        self.assertEqual(result.output_payload["row_limit_removed_row_count"], 1)
        self.assertNotIn("db_row_limit", result.artifacts[0].summary)

    def test_mysql_syntax_error_is_classified_repairable(self) -> None:
        def runner(sql: str):
            raise RuntimeError("(pymysql.err.ProgrammingError) (1064, \"You have an error in your SQL syntax near 'FROM'\")")

        capability = SQLQuerySQLExecuteReadonlyCapability(adapter=MySQLReadonlyAdapter(runner=runner))
        request = make_request(
            "skill.sql_query",
            dependency_outputs={"guard": {"sql": "SELECT FROM variety", "guard_pass_token": "guard:ok"}},
        )

        result = asyncio.run(capability.execute(request))

        self.assertIsNotNone(result.error)
        assert result.error is not None
        self.assertEqual(result.error.code, "db_sql_syntax_error")
        self.assertTrue(result.error.metadata["repairable_sql_error"])
        self.assertEqual(result.error.metadata["failed_stage"], "sql_execute_readonly")
        self.assertIn("sql_fingerprint", result.error.metadata)

    def test_unknown_column_is_classified_repairable(self) -> None:
        def runner(sql: str):
            raise RuntimeError("(1054, \"Unknown column 'bad_column' in 'field list'\")")

        capability = SQLQuerySQLExecuteReadonlyCapability(adapter=MySQLReadonlyAdapter(runner=runner))
        request = make_request(
            "skill.sql_query",
            dependency_outputs={"guard": {"sql": "SELECT bad_column FROM variety", "guard_pass_token": "guard:ok"}},
        )

        result = asyncio.run(capability.execute(request))

        self.assertIsNotNone(result.error)
        assert result.error is not None
        self.assertEqual(result.error.code, "db_unknown_column")
        self.assertTrue(result.error.metadata["repairable_sql_error"])

    def test_uncertain_db_error_defaults_not_repairable(self) -> None:
        def runner(sql: str):
            raise RuntimeError("database returned an unexpected domain-specific failure")

        capability = SQLQuerySQLExecuteReadonlyCapability(adapter=MySQLReadonlyAdapter(runner=runner))
        request = make_request(
            "skill.sql_query",
            dependency_outputs={"guard": {"sql": "SELECT variety_name FROM variety", "guard_pass_token": "guard:ok"}},
        )

        result = asyncio.run(capability.execute(request))

        self.assertIsNotNone(result.error)
        assert result.error is not None
        self.assertEqual(result.error.code, "db_execution_failed")
        self.assertFalse(result.error.metadata.get("repairable_sql_error", False))
