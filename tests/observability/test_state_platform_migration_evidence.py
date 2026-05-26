from __future__ import annotations

import unittest

from src.state.migration import MigrationEvidence


class StatePlatformMigrationEvidenceTest(unittest.TestCase):
    def test_migration_evidence_public_report_redacts_secret_and_tracks_pending_gates(self) -> None:
        evidence = MigrationEvidence(
            migration_id="mig-1",
            status="pending",
            row_counts={"conversation": 10},
            checksums={"conversation": "sha256:abc"},
            pending_gates=("postgres_test_dsn_not_configured", "operator_confirmation_missing"),
            metadata={"dsn": "postgresql_fixture_dsn", "token": "<fixture>"},
        )
        public = evidence.public_dict()
        self.assertIn("postgres_test_dsn_not_configured", public["pending_gates"])
        self.assertNotIn("postgresql://", repr(public))
        self.assertNotIn("abc", repr(public["metadata"]))

    def test_nested_migration_metadata_is_recursively_redacted(self) -> None:
        evidence = MigrationEvidence(
            migration_id="mig-2",
            status="pending",
            metadata={"nested": {"dsn": "postgresql_fixture_dsn", "token": "nested-token"}},
        )
        public = evidence.public_dict()
        self.assertNotIn("postgresql_fixture_dsn", repr(public))
        self.assertNotIn("nested-token", repr(public))
