from __future__ import annotations

import unittest

from src.core.enums import ArtifactType, EventVisibility
from src.core.models import Artifact, EventRecord
from src.orchestration.answer_selection import select_final_text_artifact
from src.orchestration.answer_roles import RESPONSE_ROLE_FINAL


class AnswerSelectionTest(unittest.TestCase):
    def test_selects_final_role_text_artifact_before_intermediate_text(self) -> None:
        artifacts = [
            Artifact(
                "node-intermediate:main_agent_response:intermediate:abc",
                "task-1",
                "node-intermediate",
                ArtifactType.TEXT,
                "局部回答",
                is_complete=True,
            ),
            Artifact(
                "node-final:main_agent_response:final:def",
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

    def test_can_fall_back_to_final_role_event_for_legacy_roleless_artifacts(self) -> None:
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
                event_type="main_agent.output_final",
                payload={"response_role": RESPONSE_ROLE_FINAL},
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
