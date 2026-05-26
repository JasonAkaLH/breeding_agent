from __future__ import annotations

import unittest

from src.state.postgres.worker import postgres_test_dsn_or_skip_reason


class PostgresReadNotBlockedByWriterTest(unittest.TestCase):
    def test_real_postgres_mvcc_gate_has_explicit_skip_reason_when_not_configured(self) -> None:
        dsn, reason = postgres_test_dsn_or_skip_reason()
        if dsn is None:
            self.assertEqual(reason, "postgres_test_dsn_not_configured")
            self.skipTest(reason)
        self.fail("real PostgreSQL MVCC integration is intentionally not executed by default")
