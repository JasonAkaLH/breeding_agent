from __future__ import annotations

import unittest

from src.state.cutover import FreshCutoverInput, build_postgres_fresh_cutover_plan


class StatePlatformCutoverEvidenceTest(unittest.TestCase):
    def test_cutover_evidence_public_report_redacts_secret_and_has_no_sqlite_counts(self) -> None:
        evidence = build_postgres_fresh_cutover_plan(
            FreshCutoverInput(
                postgres_dsn="postgresql://user:secret@example/db",
                schema_ready=True,
                runtime_smoke_ready=True,
                queue_backlog=0,
                dead_letter_count=0,
                sqlite_history_abandoned=True,
                metadata={"dsn": "postgresql_fixture_dsn", "token": "nested-token"},
            )
        )
        public = evidence.public_dict()
        self.assertTrue(public["ready"])
        self.assertNotIn("row_counts", public)
        self.assertNotIn("checksums", public)
        self.assertNotIn("postgresql://", repr(public))
        self.assertNotIn("nested-token", repr(public))

    def test_nested_cutover_metadata_is_recursively_redacted(self) -> None:
        evidence = build_postgres_fresh_cutover_plan(
            FreshCutoverInput(
                postgres_dsn="postgresql_fixture_dsn",
                schema_ready=False,
                runtime_smoke_ready=True,
                queue_backlog=0,
                dead_letter_count=0,
                sqlite_history_abandoned=True,
                metadata={"nested": {"dsn": "postgresql_fixture_dsn", "token": "nested-token"}},
            )
        )
        public = evidence.public_dict()
        self.assertNotIn("postgresql_fixture_dsn", repr(public))
        self.assertNotIn("nested-token", repr(public))
