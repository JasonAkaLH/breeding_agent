from __future__ import annotations

import unittest
from uuid import uuid4

from sqlalchemy import text

from src.storage.postgres import create_postgres_engine
from tests.postgres_test_support import isolated_postgres_test_dsn_or_skip_reason


class PostgresReadNotBlockedByWriterTest(unittest.TestCase):
    def test_real_postgres_mvcc_gate_has_explicit_skip_reason_when_not_configured(self) -> None:
        dsn, reason = isolated_postgres_test_dsn_or_skip_reason(
            "MAF_POSTGRES_MVCC_TEST_DSN",
            fallback_env="MAF_POSTGRES_TEST_DSN",
        )
        if dsn is None:
            self.assertEqual(reason, "maf_postgres_mvcc_test_dsn_not_configured")
            self.skipTest(reason)
        engine = create_postgres_engine(str(dsn))
        row_id = uuid4().hex
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "CREATE TABLE IF NOT EXISTS maf_mvcc_gate "
                        "(row_id text PRIMARY KEY, value integer NOT NULL)"
                    )
                )
                connection.execute(
                    text("INSERT INTO maf_mvcc_gate (row_id, value) VALUES (:row_id, 1)"),
                    {"row_id": row_id},
                )
            with engine.connect() as writer, engine.connect() as reader:
                transaction = writer.begin()
                try:
                    writer.execute(
                        text("UPDATE maf_mvcc_gate SET value = 2 WHERE row_id = :row_id"),
                        {"row_id": row_id},
                    )
                    reader.execute(text("SET LOCAL statement_timeout = '2s'"))
                    value = reader.scalar(
                        text("SELECT value FROM maf_mvcc_gate WHERE row_id = :row_id"),
                        {"row_id": row_id},
                    )
                    self.assertEqual(value, 1)
                finally:
                    transaction.rollback()
            with engine.begin() as connection:
                connection.execute(
                    text("DELETE FROM maf_mvcc_gate WHERE row_id = :row_id"),
                    {"row_id": row_id},
                )
        finally:
            engine.dispose()
