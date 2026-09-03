from __future__ import annotations

import inspect
import tempfile
import unittest
from pathlib import Path

from src.capabilities.skill_tool.executor import SkillExecutor
from src.core.contracts import CapabilityExecutionRequest
from src.integrations.agent_skills import SkillRuntimeState


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


class AgentSkillExecutorRevisionTest(unittest.IsolatedAsyncioTestCase):
    async def test_non_v2_revision_never_falls_back_to_active_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "skills"
            root.mkdir()
            state = SkillRuntimeState.from_roots(
                skill_roots=(root,),
                public_skill_roots=(root,),
                reserved_capability_ids=("main_agent.respond",),
            )
            executor = SkillExecutor(runtime_state=state)

            cases = (
                (None, "agent_skill_bundle_revision_retired"),
                ("", "agent_skill_bundle_revision_retired"),
                (
                    "skillrev-000001-aaaaaaaaaaaa",
                    "agent_skill_bundle_revision_retired",
                ),
                ("skillrev-forged", "agent_skill_bundle_revision_invalid"),
                (
                    "skillrev-v2-" + ("a" * 64),
                    "agent_skill_bundle_revision_unavailable",
                ),
            )
            for revision, expected_code in cases:
                with self.subTest(revision=revision):
                    result = await executor.execute(
                        CapabilityExecutionRequest(
                            capability_id="skill.missing",
                            conversation_id="conversation",
                            task_id="task",
                            node_id="node",
                            metadata={"skill_bundle_revision": revision},
                        )
                    )
                    self.assertIsNotNone(result.error)
                    self.assertEqual(result.error.code, expected_code)
