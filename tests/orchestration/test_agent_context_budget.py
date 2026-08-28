from __future__ import annotations

import unittest

from src.orchestration.agent_loop.context_budget import (
    AGENT_CONTEXT_BUDGET_POLICY_REVISION,
    AgentContextBudget,
)


class AgentContextBudgetTest(unittest.TestCase):
    def test_builds_exact_ninety_percent_budget(self) -> None:
        budget = AgentContextBudget.from_model_context_window(450_000)

        self.assertEqual(
            budget.to_payload(),
            {
                "compact_threshold_percent": 90,
                "model_context_window_tokens": 450_000,
                "policy_revision": AGENT_CONTEXT_BUDGET_POLICY_REVISION,
                "total_context_limit_tokens": 405_000,
            },
        )
        self.assertEqual(
            AgentContextBudget.from_payload(budget.to_payload()),
            budget,
        )
        self.assertEqual(budget.digest, AgentContextBudget.from_model_context_window(450_000).digest)

    def test_rejects_invalid_or_drifted_budget(self) -> None:
        valid = AgentContextBudget.from_model_context_window(101).to_payload()
        cases = (
            None,
            {},
            {**valid, "unexpected": 1},
            {**valid, "compact_threshold_percent": 89},
            {**valid, "model_context_window_tokens": True},
            {**valid, "model_context_window_tokens": 0},
            {**valid, "total_context_limit_tokens": 91},
            {**valid, "policy_revision": "unknown"},
        )

        for value in cases:
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "agent_context_budget_invalid"):
                    AgentContextBudget.from_payload(value)
