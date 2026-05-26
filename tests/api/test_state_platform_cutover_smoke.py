from __future__ import annotations

import unittest

from src.state.migration import CutoverReadiness, evaluate_cutover_readiness


class StatePlatformCutoverSmokeTest(unittest.TestCase):
    def test_cutover_smoke_requires_migration_shadow_and_queue_drain(self) -> None:
        readiness = evaluate_cutover_readiness(
            dry_run_passed=True,
            validation_passed=True,
            shadow_compare_passed=False,
            queue_backlog=1,
            dead_letter_count=0,
        )
        self.assertFalse(readiness.ready)
        self.assertIn("shadow_compare_not_passed", readiness.blockers)
        self.assertIn("queue_not_drained", readiness.blockers)
        self.assertIsInstance(readiness, CutoverReadiness)
