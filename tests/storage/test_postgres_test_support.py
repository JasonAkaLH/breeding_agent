from __future__ import annotations

import unittest
from unittest.mock import patch

from tests.postgres_test_support import isolated_postgres_test_dsn_or_skip_reason


class IsolatedPostgresTestDSNTest(unittest.TestCase):
    def test_primary_dsn_wins_over_legacy_fallback(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "MAF_POSTGRES_MODULE_TEST_DSN": "postgresql+psycopg://module",
                "MAF_POSTGRES_TEST_DSN": "postgresql+psycopg://legacy",
            },
            clear=True,
        ):
            dsn, reason = isolated_postgres_test_dsn_or_skip_reason(
                "MAF_POSTGRES_MODULE_TEST_DSN",
                fallback_env="MAF_POSTGRES_TEST_DSN",
            )

        self.assertEqual(dsn, "postgresql+psycopg://module")
        self.assertIsNone(reason)

    def test_legacy_fallback_remains_supported(self) -> None:
        with patch.dict(
            "os.environ",
            {"MAF_POSTGRES_TEST_DSN": "postgresql+psycopg://legacy"},
            clear=True,
        ):
            dsn, reason = isolated_postgres_test_dsn_or_skip_reason(
                "MAF_POSTGRES_MODULE_TEST_DSN",
                fallback_env="MAF_POSTGRES_TEST_DSN",
            )

        self.assertEqual(dsn, "postgresql+psycopg://legacy")
        self.assertIsNone(reason)

    def test_missing_dsn_reports_the_primary_contract(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            dsn, reason = isolated_postgres_test_dsn_or_skip_reason(
                "MAF_POSTGRES_MODULE_TEST_DSN",
                fallback_env="MAF_POSTGRES_TEST_DSN",
            )

        self.assertIsNone(dsn)
        self.assertEqual(reason, "maf_postgres_module_test_dsn_not_configured")
