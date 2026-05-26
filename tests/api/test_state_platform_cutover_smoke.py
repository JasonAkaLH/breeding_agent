from __future__ import annotations

import unittest

from src.state.cutover import FreshCutoverPlan, FreshCutoverInput, build_postgres_fresh_cutover_plan


class StatePlatformCutoverSmokeTest(unittest.TestCase):
    def test_fresh_cutover_smoke_requires_schema_runtime_and_queue_health(self) -> None:
        readiness = build_postgres_fresh_cutover_plan(
            FreshCutoverInput(
                postgres_dsn="postgresql_fixture_dsn",
                schema_ready=True,
                runtime_smoke_ready=False,
                queue_backlog=1,
                dead_letter_count=0,
                sqlite_history_abandoned=True,
            )
        )
        self.assertFalse(readiness.ready)
        self.assertIn("runtime_smoke_not_ready", readiness.blockers)
        self.assertIn("queue_not_drained", readiness.blockers)
        self.assertIsInstance(readiness, FreshCutoverPlan)
