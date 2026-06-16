from __future__ import annotations

import unittest

from src.orchestration.backpressure import BackpressureGuard, BackpressureRejected


class BackpressureGuardTest(unittest.TestCase):
    def test_strict_reject_when_active_task_limit_reached(self) -> None:
        guard = BackpressureGuard(max_active_tasks=1)

        with self.assertRaises(BackpressureRejected):
            guard.ensure_can_accept(active_task_count=1)
