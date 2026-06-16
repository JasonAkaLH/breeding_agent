from __future__ import annotations

import unittest

from src.storage.postgres.session import create_postgres_engine

from src.state.cutover import (
    FreshCutoverInput,
    build_postgres_fresh_cutover_plan,
    validate_cutover_report_is_redacted,
)


class PostgresFreshCutoverTest(unittest.TestCase):
    def test_cutover_plan_does_not_require_sqlite_migration_evidence(self) -> None:
        plan = build_postgres_fresh_cutover_plan(
            FreshCutoverInput(
                postgres_dsn="postgresql://user:secret@example/db",
                schema_ready=True,
                runtime_smoke_ready=True,
                queue_backlog=0,
                dead_letter_count=0,
                sqlite_history_abandoned=True,
            )
        )
        public = plan.public_dict()
        self.assertTrue(plan.ready)
        self.assertNotIn("sqlite_row_counts", public)
        self.assertNotIn("sqlite_checksums", public)
        self.assertNotIn("postgresql://", repr(public))
        self.assertTrue(validate_cutover_report_is_redacted(public))

    def test_cutover_blocks_if_sqlite_history_not_explicitly_abandoned(self) -> None:
        plan = build_postgres_fresh_cutover_plan(
            FreshCutoverInput(
                postgres_dsn="postgresql_fixture_dsn",
                schema_ready=True,
                runtime_smoke_ready=True,
                queue_backlog=0,
                dead_letter_count=0,
                sqlite_history_abandoned=False,
            )
        )
        self.assertFalse(plan.ready)
        self.assertIn("sqlite_history_abandonment_not_confirmed", plan.blockers)

    def test_cutover_blocks_on_queue_or_dead_letters(self) -> None:
        plan = build_postgres_fresh_cutover_plan(
            FreshCutoverInput(
                postgres_dsn="postgresql_fixture_dsn",
                schema_ready=True,
                runtime_smoke_ready=True,
                queue_backlog=1,
                dead_letter_count=2,
                sqlite_history_abandoned=True,
            )
        )
        self.assertFalse(plan.ready)
        self.assertIn("queue_not_drained", plan.blockers)
        self.assertIn("dead_letter_not_empty", plan.blockers)

    def test_postgres_engine_hides_parameters_in_errors(self) -> None:
        engine = create_postgres_engine("postgresql+psycopg://user:secret@example.invalid/db")
        self.assertTrue(engine.hide_parameters)
