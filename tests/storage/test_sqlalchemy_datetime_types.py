from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from sqlalchemy.dialects import postgresql, sqlite

from src.storage import sqlalchemy_base


class SQLAlchemyDateTimeTypesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.postgresql = postgresql.dialect()
        self.sqlite = sqlite.dialect()

    def test_datetime_text_binds_naive_utc_per_dialect(self) -> None:
        value = datetime(2026, 8, 31, 3, 22, 12, 836008)
        kind = sqlalchemy_base.DateTimeText()

        self.assertEqual(
            kind.process_bind_param(value, self.postgresql),
            value.replace(tzinfo=timezone.utc),
        )
        self.assertEqual(kind.process_bind_param(value, self.sqlite), value.isoformat())

    def test_datetime_text_rejects_aware_bind(self) -> None:
        value = datetime(2026, 8, 31, 3, 22, tzinfo=timezone.utc)
        kind = sqlalchemy_base.DateTimeText()

        for dialect in (self.postgresql, self.sqlite):
            with self.subTest(dialect=dialect.name):
                with self.assertRaises(ValueError):
                    kind.process_bind_param(value, dialect)

    def test_datetime_text_results_are_utc_naive(self) -> None:
        kind = sqlalchemy_base.DateTimeText()
        offset = timezone(timedelta(hours=8))
        aware = datetime(2026, 8, 31, 11, 22, 12, 836008, tzinfo=offset)
        expected = datetime(2026, 8, 31, 3, 22, 12, 836008)

        self.assertEqual(
            kind.process_result_value(aware, self.postgresql),
            expected,
        )
        self.assertEqual(
            kind.process_result_value(aware.isoformat(), self.sqlite),
            expected,
        )
        self.assertEqual(
            kind.process_result_value(expected.isoformat(), self.sqlite),
            expected,
        )

    def test_aware_utc_datetime_text_requires_aware_and_normalizes_to_utc(self) -> None:
        kind = getattr(sqlalchemy_base, "AwareUTCDateTimeText")()
        offset = timezone(timedelta(hours=8))
        aware = datetime(2026, 8, 31, 11, 22, 12, 836008, tzinfo=offset)
        expected = datetime(2026, 8, 31, 3, 22, 12, 836008, tzinfo=timezone.utc)

        self.assertEqual(kind.process_bind_param(aware, self.postgresql), expected)
        self.assertEqual(
            kind.process_bind_param(aware, self.sqlite),
            expected.isoformat(),
        )
        self.assertEqual(kind.process_result_value(aware, self.postgresql), expected)
        self.assertEqual(
            kind.process_result_value(aware.isoformat(), self.sqlite),
            expected,
        )

        naive = aware.replace(tzinfo=None)
        for dialect in (self.postgresql, self.sqlite):
            with self.subTest(dialect=dialect.name, operation="bind"):
                with self.assertRaises(ValueError):
                    kind.process_bind_param(naive, dialect)
            with self.subTest(dialect=dialect.name, operation="result"):
                raw = naive if dialect.name == "postgresql" else naive.isoformat()
                with self.assertRaises(ValueError):
                    kind.process_result_value(raw, dialect)

    def test_datetime_types_preserve_none(self) -> None:
        kinds = (
            sqlalchemy_base.DateTimeText(),
            getattr(sqlalchemy_base, "AwareUTCDateTimeText")(),
        )
        for kind in kinds:
            for dialect in (self.postgresql, self.sqlite):
                with self.subTest(kind=type(kind).__name__, dialect=dialect.name):
                    self.assertIsNone(kind.process_bind_param(None, dialect))
                    self.assertIsNone(kind.process_result_value(None, dialect))


if __name__ == "__main__":
    unittest.main()
