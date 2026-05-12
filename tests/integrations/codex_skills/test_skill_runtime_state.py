from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.integrations.codex_skills.skill_runtime_state import SkillRuntimeState


class SkillRuntimeStateTest(unittest.TestCase):
    def _write_skill(self, root: Path, name: str, description: str) -> None:
        skill_dir = root / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(
            f"""---
name: {name}
description: {description}
---

# {name}
Body.
""",
            encoding="utf-8",
        )

    def test_refresh_builds_new_active_bundle_and_retains_old_revision(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "skill"
            self._write_skill(root, "demo-skill", "version one")
            state = SkillRuntimeState.from_roots(
                skill_roots=(root,),
                public_skill_roots=(root,),
                reserved_capability_ids=("main_agent.respond", "sql_query.query"),
            )
            first_revision = state.active_revision
            state.retain_revision(first_revision)

            self._write_skill(root, "demo-skill", "version two")
            result = state.refresh_if_changed(reason="conversation_start")

            self.assertEqual(result.status, "completed")
            self.assertNotEqual(state.active_revision, first_revision)
            old_manifest = state.catalog_for_revision(first_revision).get("demo-skill")
            new_manifest = state.active_bundle.catalog.get("demo-skill")
            self.assertIsNotNone(old_manifest)
            self.assertIsNotNone(new_manifest)
            self.assertEqual(old_manifest.description, "version one")
            self.assertEqual(new_manifest.description, "version two")

    def test_refresh_removes_deleted_public_skill_from_active_descriptors(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "skill"
            self._write_skill(root, "demo-skill", "version one")
            state = SkillRuntimeState.from_roots(
                skill_roots=(root,),
                public_skill_roots=(root,),
                reserved_capability_ids=("main_agent.respond", "sql_query.query"),
            )
            first_revision = state.active_revision
            state.retain_revision(first_revision)
            self.assertIn("skill.demo_skill", state.active_skill_capability_ids())

            (root / "demo-skill" / "SKILL.md").unlink()
            result = state.refresh_if_changed(reason="conversation_start")

            self.assertEqual(result.status, "completed")
            self.assertNotIn("skill.demo_skill", state.active_skill_capability_ids())
            self.assertIsNotNone(state.catalog_for_revision(first_revision).get("demo-skill"))

    def test_refresh_failure_keeps_previous_active_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "skill"
            self._write_skill(root, "demo-skill", "version one")
            state = SkillRuntimeState.from_roots(
                skill_roots=(root,),
                public_skill_roots=(root,),
                reserved_capability_ids=("main_agent.respond", "sql_query.query"),
            )
            first_revision = state.active_revision

            with patch(
                "src.integrations.codex_skills.skill_runtime_state.SkillCatalog.from_roots",
                side_effect=RuntimeError("boom"),
            ):
                result = state.refresh_if_changed(reason="conversation_start", force=True)

            self.assertEqual(result.status, "failed")
            self.assertEqual(result.error_type, "RuntimeError")
            self.assertEqual(state.active_revision, first_revision)
            self.assertIn("skill.demo_skill", state.active_skill_capability_ids())


if __name__ == "__main__":
    unittest.main()
