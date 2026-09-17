from __future__ import annotations

import unittest

from src.core.enums import ArtifactType, EventVisibility
from src.core.models import Artifact, EventRecord
from src.orchestration.answer_selection import select_final_text_artifact


class AnswerSelectionTest(unittest.TestCase):
    def test_selects_agent_final_artifact_before_capability_text(self) -> None:
        artifacts = [
            Artifact(
                "artifact-capability",
                "task-1",
                "node-intermediate",
                ArtifactType.TEXT,
                "局部回答",
                is_complete=True,
            ),
            Artifact(
                "agent-artifact:task-1:final",
                "task-1",
                "node-final",
                ArtifactType.TEXT,
                "全局汇总",
                is_complete=True,
            ),
        ]

        selected = select_final_text_artifact(artifacts)

        self.assertIsNotNone(selected)
        self.assertEqual(selected.storage_ref, "全局汇总")

    def test_can_select_final_node_from_agent_final_event(self) -> None:
        artifacts = [
            Artifact("art-intermediate", "task-1", "node-intermediate", ArtifactType.TEXT, "局部回答", is_complete=True),
            Artifact("art-final", "task-1", "node-final", ArtifactType.TEXT, "全局汇总", is_complete=True),
        ]
        events = [
            EventRecord(
                "evt-final",
                "conv-1",
                "task-1",
                node_id="node-final",
                event_type="agent.final_output",
                payload={"artifact_id": "art-final", "message_id": "message-final"},
                visibility=EventVisibility.FRONTEND,
            )
        ]

        selected = select_final_text_artifact(artifacts, events=events)

        self.assertIsNotNone(selected)
        self.assertEqual(selected.artifact_id, "art-final")

    def test_falls_back_to_first_text_artifact_without_roles(self) -> None:
        artifacts = [
            Artifact("art-old", "task-1", "node-old", ArtifactType.TEXT, "旧回答", is_complete=True),
            Artifact("art-other", "task-1", "node-other", ArtifactType.TEXT, "另一个回答", is_complete=True),
        ]

        selected = select_final_text_artifact(artifacts)

        self.assertIsNotNone(selected)
        self.assertEqual(selected.artifact_id, "art-old")


if __name__ == "__main__":
    unittest.main()
