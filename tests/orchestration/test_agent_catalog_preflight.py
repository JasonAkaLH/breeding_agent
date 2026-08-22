from __future__ import annotations

import unittest

from src.orchestration.agent_loop.tool_catalog import (
    AgentCatalogPreflight,
    AgentToolCatalog,
    CatalogPreflightDecision,
)
from src.orchestration.agent_loop.models import AgentToolDescriptor


class AgentCatalogPreflightTest(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = AgentToolCatalog(
            tools=(
                AgentToolDescriptor.for_capability(
                    "skill.lookup",
                    description="lookup",
                    input_schema={
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                ),
            ),
            policies={},
        )
        self.preflight = AgentCatalogPreflight(token_estimator=len)

    def _evaluate(self, **overrides):
        values = {
            "catalog": self.catalog,
            "stable_rules": "rules",
            "safe_tool_rules": "tools",
            "current_user_input": "user",
            "minimum_suffix": "suffix",
            "history_segments": (),
            "eligible_compactable_ranges": 0,
            "token_budget": 1000,
        }
        values.update(overrides)
        return self.preflight.evaluate(**values)

    def test_fits_includes_complete_schema_budget_without_exposing_schema(self) -> None:
        result = self._evaluate()
        self.assertEqual(result.decision, CatalogPreflightDecision.FITS)
        self.assertEqual(result.tool_count, 1)
        self.assertGreater(result.schema_bytes, 0)
        self.assertNotIn("query", repr(result))

    def test_history_only_overflow_requires_compaction_when_range_exists(self) -> None:
        baseline = self._evaluate()
        result = self._evaluate(
            history_segments=("h" * 100,),
            eligible_compactable_ranges=1,
            token_budget=baseline.required_tokens + 10,
        )
        self.assertEqual(
            result.decision,
            CatalogPreflightDecision.HISTORY_COMPACTION_REQUIRED,
        )

    def test_no_eligible_range_or_required_overflow_is_fatal(self) -> None:
        baseline = self._evaluate()
        no_range = self._evaluate(
            history_segments=("history",),
            token_budget=baseline.required_tokens,
        )
        required = self._evaluate(token_budget=baseline.required_tokens - 1)
        self.assertEqual(
            no_range.decision,
            CatalogPreflightDecision.FATAL_REQUIRED_SEGMENTS_TOO_LARGE,
        )
        self.assertEqual(
            required.decision,
            CatalogPreflightDecision.FATAL_REQUIRED_SEGMENTS_TOO_LARGE,
        )
