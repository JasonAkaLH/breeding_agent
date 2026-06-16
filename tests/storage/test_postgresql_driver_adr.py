from __future__ import annotations

import pathlib
import unittest

ADR = pathlib.Path("docs/prd/backend/postgresql-state-platform/adr-postgresql-driver.md")


class PostgreSQLDriverAdrTest(unittest.TestCase):
    def test_driver_adr_records_required_decision_evidence(self) -> None:
        text = ADR.read_text(encoding="utf-8")
        lowered = text.lower()
        for required in (
            "psycopg",
            "asyncpg",
            "sqlalchemy",
            "python 3.13",
            "license",
            "sqlstate",
            "statement timeout",
            "cancel",
            "pool",
        ):
            with self.subTest(required=required):
                self.assertIn(required, lowered)
        self.assertIn("Decision", text)
        self.assertIn("Accepted", text)
        self.assertIn("Rejected", text)
        self.assertIn("MAF_POSTGRES_STATE_DSN", text)


if __name__ == "__main__":
    unittest.main()
