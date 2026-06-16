from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from src.integrations.rust_safety_contract import resource_limit
from src.mysql_engine import MYSQL_READONLY_URL_ENV, build_sql_engine


class MySQLEngineConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self._previous_env_url = os.environ.pop(MYSQL_READONLY_URL_ENV, None)

    def tearDown(self) -> None:
        os.environ.pop(MYSQL_READONLY_URL_ENV, None)
        if self._previous_env_url is not None:
            os.environ[MYSQL_READONLY_URL_ENV] = self._previous_env_url

    def test_build_sql_engine_requires_explicit_local_config(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "mysql_readonly.url"):
            build_sql_engine(config={})

    def test_build_sql_engine_uses_config_yaml_readonly_url(self) -> None:
        engine = build_sql_engine(
            config={
                "mysql_readonly": {
                    "url": "mysql+pymysql://readonly@example.invalid:3306/business?charset=utf8mb4",
                    "pool_size": 2,
                    "max_overflow": 3,
                    "connect_timeout": 4,
                    "read_timeout": 5,
                    "execution_timeout": 6,
                }
            }
        )
        try:
            self.assertEqual(engine.url.host, "example.invalid")
            self.assertEqual(engine.url.database, "business")
            self.assertEqual(engine.url.username, "readonly")
            self.assertIsNone(engine.url.password)
        finally:
            engine.dispose()

    def test_build_sql_engine_defaults_db_timeouts_from_safety_contract(self) -> None:
        with patch("src.mysql_engine.create_engine") as create_engine:
            create_engine.return_value = object()

            build_sql_engine(
                config={"mysql_readonly": {"url": "mysql+pymysql://readonly@example.invalid:3306/business"}}
            )

        kwargs = create_engine.call_args.kwargs
        deadline_seconds = resource_limit("db_deadline_ms") // 1000
        self.assertEqual(kwargs["connect_args"]["read_timeout"], deadline_seconds)
        self.assertEqual(kwargs["execution_options"]["timeout"], deadline_seconds)

    def test_build_sql_engine_clamps_db_timeouts_to_safety_hard_cap(self) -> None:
        with patch("src.mysql_engine.create_engine") as create_engine:
            create_engine.return_value = object()

            build_sql_engine(
                config={
                    "mysql_readonly": {
                        "url": "mysql+pymysql://readonly@example.invalid:3306/business",
                        "read_timeout": 60,
                        "execution_timeout": 60,
                    }
                }
            )

        kwargs = create_engine.call_args.kwargs
        hard_cap_seconds = resource_limit("db_hard_cap_ms") // 1000
        self.assertEqual(kwargs["connect_args"]["read_timeout"], hard_cap_seconds)
        self.assertEqual(kwargs["execution_options"]["timeout"], hard_cap_seconds)

    def test_build_sql_engine_can_build_url_from_config_parts(self) -> None:
        engine = build_sql_engine(
            config={
                "mysql_readonly": {
                    "host": "db.internal.example",
                    "port": 3307,
                    "database": "business",
                    "username": "readonly",
                    "password": "test_password",
                    "charset": "utf8mb4",
                }
            }
        )
        try:
            self.assertEqual(engine.url.host, "db.internal.example")
            self.assertEqual(engine.url.port, 3307)
            self.assertEqual(engine.url.database, "business")
            self.assertEqual(engine.url.username, "readonly")
            self.assertEqual(engine.url.password, "test_password")
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
