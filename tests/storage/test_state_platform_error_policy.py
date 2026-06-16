from __future__ import annotations

import unittest

from src.state.errors import classify_state_error, extract_sqlstate, redact_text


class FakeDriverError(Exception):
    def __init__(self, message: str, *, sqlstate: str | None = None, pgcode: str | None = None) -> None:
        super().__init__(message)
        self.sqlstate = sqlstate
        self.pgcode = pgcode


class WrappedError(Exception):
    def __init__(self, original: Exception) -> None:
        super().__init__("wrapped")
        self.orig = original


class StatePlatformErrorPolicyTest(unittest.TestCase):
    def test_retryable_postgresql_sqlstates_are_allowlisted(self) -> None:
        for sqlstate in ("40P01", "40001", "55P03", "57014"):
            error = classify_state_error(FakeDriverError("driver", sqlstate=sqlstate), operation="claim")
            self.assertTrue(error.retryable, sqlstate)
            self.assertEqual(error.sqlstate, sqlstate)
            self.assertIn(error.code, {"postgres_deadlock", "postgres_serialization_failure", "postgres_lock_not_available", "postgres_query_canceled"})

    def test_unknown_errors_fail_closed_and_are_redacted(self) -> None:
        error = classify_state_error(FakeDriverError("postgres_fixture_dsn token=<fixture>", sqlstate="XX999"), operation="write")
        self.assertFalse(error.retryable)
        self.assertEqual(error.code, "state_platform_error")
        dumped = repr(error.public_dict())
        self.assertNotIn("user:pass", dumped)
        self.assertNotIn("token=<fixture>", dumped)

    def test_sqlstate_extractor_supports_driver_wrappers(self) -> None:
        direct = FakeDriverError("deadlock", sqlstate="40P01")
        pgcode = FakeDriverError("deadlock", pgcode="40P01")
        wrapped = WrappedError(pgcode)
        self.assertEqual(extract_sqlstate(direct), "40P01")
        self.assertEqual(extract_sqlstate(pgcode), "40P01")
        self.assertEqual(extract_sqlstate(wrapped), "40P01")

    def test_redact_text_removes_secrets_without_erasing_signal(self) -> None:
        text = "dsn=<fixture-dsn> api_token=<fixture> password=<fixture>"
        redacted = redact_text(text)
        self.assertIsNotNone(redacted)
        redacted_text = redacted or ""
        self.assertIn("dsn=", redacted_text)
        self.assertNotIn("user:pass", redacted_text)
        self.assertNotIn("abcdef", redacted_text)
        self.assertNotIn("hunter2", redacted_text)


if __name__ == "__main__":
    unittest.main()
