from __future__ import annotations

import os
import unittest

from src.state.postgres.worker import CLAIM_NEXT_COMMAND_SQL, postgres_test_dsn_or_skip_reason


class PostgresWorkerClaimOrderingTest(unittest.TestCase):
    def test_claim_sql_uses_skip_locked_and_prior_unfinished_guard(self) -> None:
        sql = " ".join(CLAIM_NEXT_COMMAND_SQL.split()).upper()
        self.assertIn("FOR UPDATE SKIP LOCKED", sql)
        self.assertIn("NOT EXISTS", sql)
        self.assertIn("PRIOR.PARTITION_SEQUENCE < CANDIDATE.PARTITION_SEQUENCE", sql)
        self.assertIn("LEASE_EXPIRES_AT", sql)

    def test_claim_sql_reclaims_expired_leased_commands(self) -> None:
        sql = " ".join(CLAIM_NEXT_COMMAND_SQL.split()).lower()
        self.assertIn("candidate.status = 'leased'", sql)
        self.assertIn("candidate.lease_expires_at <= now()", sql)

    def test_real_postgres_gate_reports_explicit_skip_reason_without_dsn(self) -> None:
        old = os.environ.pop("MAF_POSTGRES_TEST_DSN", None)
        try:
            dsn, reason = postgres_test_dsn_or_skip_reason()
        finally:
            if old is not None:
                os.environ["MAF_POSTGRES_TEST_DSN"] = old
        self.assertIsNone(dsn)
        self.assertEqual(reason, "postgres_test_dsn_not_configured")
