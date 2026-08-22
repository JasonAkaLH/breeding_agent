from __future__ import annotations

import inspect
import unittest

from src.capabilities.skill_tool.executor import SkillExecutor
from src.core.contracts import CapabilityExecutionRequest


class AgentSkillExecutorAdapterTest(unittest.TestCase):
    def test_trusted_user_message_overrides_model_query(self) -> None:
        request = CapabilityExecutionRequest(
            capability_id="skill.safe",
            conversation_id="conversation",
            task_id="task",
            node_id="node",
            input_payload={"query": "attacker supplied", "user_message": "attacker supplied"},
            metadata={
                "resolved_user_message": "trusted resolved message",
                "skill_bundle_revision": "revision-1",
            },
        )
        self.assertEqual(
            SkillExecutor._resolve_user_message(request),
            "trusted resolved message",
        )

    def test_delegated_mode_remains_non_executable_and_no_finalizer_is_created(self) -> None:
        source = inspect.getsource(SkillExecutor.execute)
        self.assertIn('execution.mode == "delegated_main_agent"', source)
        self.assertIn("delegated_main_agent_not_executable", source)
        self.assertNotIn("main_agent.respond", source)
        self.assertNotIn("finalizer", source.lower())

    def test_answer_modes_do_not_create_a_second_model_call(self) -> None:
        source = inspect.getsource(SkillExecutor)
        self.assertIn('execution.answer_mode == "direct"', source)
        self.assertNotIn("AgentModelPort", source)
        self.assertNotIn("LLMClient", source)
        self.assertNotIn("main_agent.respond", source)
