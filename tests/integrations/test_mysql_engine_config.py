from __future__ import annotations

import os
import unittest

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
