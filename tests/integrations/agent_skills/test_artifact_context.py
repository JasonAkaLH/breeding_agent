from __future__ import annotations

import unittest

from src.integrations.agent_skills.execution import (
    build_skill_artifact_context,
    build_skill_safe_metadata,
    build_skill_script_artifact_context,
)


class SkillArtifactContextTest(unittest.TestCase):
    def test_llm_artifact_context_excludes_raw_content(self) -> None:
        context = build_skill_artifact_context(
            {
                "uploaded_artifacts": [
                    {
                        "upload_id": "upl-1",
                        "filename": "materials.csv",
                        "content": "plot_id,hyb_check,set\n1,A,A\n",
                        "preview": {"row_count": 1},
                    }
                ]
            }
        )

        self.assertEqual(context[0]["filename"], "materials.csv")
        self.assertEqual(context[0]["preview"], {"row_count": 1})
        self.assertNotIn("content", context[0])

    def test_script_artifact_context_uses_raw_skill_artifacts(self) -> None:
        context = build_skill_script_artifact_context(
            {
                "uploaded_artifacts": [
                    {
                        "upload_id": "upl-1",
                        "filename": "materials.csv",
                        "preview": {"row_count": 1},
                    }
                ],
                "skill_artifacts": [
                    {
                        "upload_id": "upl-1",
                        "filename": "materials.csv",
                        "content": "plot_id,hyb_check,set\n1,A,A\n",
                        "preview": {"row_count": 1},
                    }
                ],
            },
            fallback_artifact_context=(),
        )

        self.assertEqual(context[0]["filename"], "materials.csv")
        self.assertEqual(context[0]["content"], "plot_id,hyb_check,set\n1,A,A\n")

    def test_script_artifact_context_falls_back_to_summary_when_raw_artifact_is_absent(self) -> None:
        fallback = ({"upload_id": "upl-1", "filename": "materials.csv", "preview": {"row_count": 1}},)

        context = build_skill_script_artifact_context({}, fallback_artifact_context=fallback)

        self.assertEqual(context, fallback)

    def test_script_artifact_context_preserves_legacy_raw_uploaded_artifacts(self) -> None:
        context = build_skill_script_artifact_context(
            {
                "uploaded_artifacts": [
                    {
                        "upload_id": "upl-1",
                        "filename": "materials.csv",
                        "content": "plot_id,hyb_check,set\n1,A,A\n",
                        "content_base64": "cGxvdF9pZA==",
                        "preview": {"row_count": 1},
                    }
                ]
            },
            fallback_artifact_context=(),
        )

        self.assertEqual(context[0]["content"], "plot_id,hyb_check,set\n1,A,A\n")
        self.assertEqual(context[0]["content_base64"], "cGxvdF9pZA==")

    def test_safe_metadata_strips_raw_artifact_content(self) -> None:
        metadata = {
            "uploaded_artifacts": [
                {
                    "upload_id": "upl-1",
                    "filename": "materials.csv",
                    "content": "plot_id,hyb_check,set\n1,A,A\n",
                }
            ],
            "skill_artifacts": [
                {
                    "upload_id": "upl-1",
                    "filename": "materials.csv",
                    "content": "plot_id,hyb_check,set\n1,A,A\n",
                }
            ],
        }

        safe = build_skill_safe_metadata(metadata)

        self.assertNotIn("content", safe["uploaded_artifacts"][0])
        self.assertNotIn("content", safe["skill_artifacts"][0])
        self.assertEqual(safe["skill_artifacts"][0]["filename"], "materials.csv")

    def test_mount_and_storage_paths_are_script_only(self) -> None:
        metadata = {
            "uploaded_artifacts": [
                {
                    "upload_id": "upl-1",
                    "filename": "materials.csv",
                    "mount_path": "/tmp/skill-run/input/upl-1__materials.csv",
                    "storage_key": "conv-1/upl-1/original",
                }
            ],
            "skill_artifacts": [
                {
                    "upload_id": "upl-1",
                    "filename": "materials.csv",
                    "storage_key": "conv-1/upl-1/original",
                    "conversation_id": "conv-1",
                }
            ],
        }

        safe_context = build_skill_artifact_context(metadata)
        script_context = build_skill_script_artifact_context(metadata)
        safe_metadata = build_skill_safe_metadata(metadata)

        self.assertNotIn("mount_path", safe_context[0])
        self.assertNotIn("storage_key", safe_context[0])
        self.assertEqual(script_context[0]["storage_key"], "conv-1/upl-1/original")
        self.assertNotIn("storage_key", safe_metadata["uploaded_artifacts"][0])
        self.assertNotIn("storage_key", safe_metadata["skill_artifacts"][0])


if __name__ == "__main__":
    unittest.main()
