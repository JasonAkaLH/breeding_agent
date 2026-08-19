from __future__ import annotations

import unittest

from src.integrations.mcp.aggregate_recovery import (
    MCPAggregateRecoveryStages,
    MCPAggregateStartupReconciler,
)


class MCPAggregateStartupReconcilerTest(unittest.IsolatedAsyncioTestCase):
    async def test_runs_closed_local_stages_in_fixed_order(self) -> None:
        observed: list[str] = []

        def stage(name: str):
            async def run() -> None:
                observed.append(name)

            return run

        names = MCPAggregateStartupReconciler._ORDER
        reconciler = MCPAggregateStartupReconciler(
            MCPAggregateRecoveryStages(
                **{name: stage(name) for name in names}
            )
        )

        completed = await reconciler.run()

        self.assertEqual(completed, names)
        self.assertEqual(tuple(observed), names)

    async def test_failure_stops_before_later_stages(self) -> None:
        observed: list[str] = []

        def stage(name: str):
            async def run() -> None:
                observed.append(name)
                if name == "reconcile_mrtr_evidence":
                    raise RuntimeError("authority conflict")

            return run

        names = MCPAggregateStartupReconciler._ORDER
        reconciler = MCPAggregateStartupReconciler(
            MCPAggregateRecoveryStages(
                **{name: stage(name) for name in names}
            )
        )

        with self.assertRaisesRegex(RuntimeError, "authority conflict"):
            await reconciler.run()

        self.assertEqual(
            tuple(observed),
            names[: names.index("reconcile_mrtr_evidence") + 1],
        )


if __name__ == "__main__":
    unittest.main()
