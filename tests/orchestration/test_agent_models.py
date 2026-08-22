from __future__ import annotations

import unittest

from src.orchestration.agent_loop.models import (
    AgentItem,
    AgentItemKind,
    AgentItemState,
    AgentModelBinding,
    AgentRun,
    AgentRunStatus,
)


class AgentModelsTest(unittest.TestCase):
    def test_run_rejects_duplicate_waiting_calls_and_invalid_sequence_boundary(self) -> None:
        binding = AgentModelBinding("edition")
        with self.assertRaisesRegex(ValueError, "unique"):
            AgentRun("run", "task", "conv", AgentRunStatus.RUNNING, binding, waiting_call_item_ids=("c1", "c1"))
        with self.assertRaisesRegex(ValueError, "precede"):
            AgentRun("run", "task", "conv", AgentRunStatus.RUNNING, binding, next_item_sequence=2, compacted_through_sequence=2)

    def test_tool_result_requires_existing_call_reference_shape(self) -> None:
        with self.assertRaisesRegex(ValueError, "source call"):
            AgentItem(
                item_id="result",
                run_id="run",
                task_id="task",
                sequence=1,
                kind=AgentItemKind.TOOL_RESULT,
                state=AgentItemState.RESERVED,
                payload_json="{}\n",
                payload_sha256="a" * 64,
            )
