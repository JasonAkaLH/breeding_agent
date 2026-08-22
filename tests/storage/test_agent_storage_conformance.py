from __future__ import annotations

import unittest

from src.orchestration.agent_loop.models import AgentItemKind, AgentRunStatus


class AgentStorageConformanceContractTest(unittest.TestCase):
    def test_closed_status_and_item_kind_values_are_frozen(self) -> None:
        self.assertEqual(
            {item.value for item in AgentRunStatus},
            {"running", "waiting_for_input", "waiting_for_dependency", "completed", "failed", "cancelled"},
        )
        self.assertEqual(
            {item.value for item in AgentItemKind},
            {"user_message", "assistant_message", "tool_call", "tool_result", "skill_activation", "context_summary", "continuation"},
        )
