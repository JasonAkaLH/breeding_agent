from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.integrations.agent_skills.skill_runtime_state import (
    SkillBundleRevisionError,
    SkillRuntimeState,
)


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
        capability_id = "skill." + name.replace("-", "_")
        (skill_dir / "skill.contract.yaml").write_text(
            f"""contract_version: '2'
capability:
  id: {capability_id}
  display_name: {name}
  description: {description}
runtime:
  mode: python_subprocess
  answer_mode: direct
entrypoints:
  run:
    path: scripts/run.py
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
                reserved_capability_ids=("main_agent.respond",),
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

    def test_revision_v2_is_stable_across_independent_states(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "skill"
            self._write_skill(root, "demo-skill", "stable")

            first = SkillRuntimeState.from_roots(
                skill_roots=(root,),
                public_skill_roots=(root,),
                reserved_capability_ids=("main_agent.respond",),
            )
            second = SkillRuntimeState.from_roots(
                skill_roots=(root,),
                public_skill_roots=(root,),
                reserved_capability_ids=("main_agent.respond",),
            )

            self.assertEqual(first.active_revision, second.active_revision)
            self.assertRegex(first.active_revision, r"^skillrev-v2-[0-9a-f]{64}$")

    def test_execution_resolver_rejects_non_v2_revisions_before_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "skill"
            self._write_skill(root, "demo-skill", "stable")
            state = SkillRuntimeState.from_roots(
                skill_roots=(root,),
                public_skill_roots=(root,),
                reserved_capability_ids=("main_agent.respond",),
            )
            legacy = "skillrev-000002-f84bc49b3ad8"
            state._bundles[legacy] = state.active_bundle

            cases = (
                (None, "agent_skill_bundle_revision_retired"),
                ("", "agent_skill_bundle_revision_retired"),
                ("   ", "agent_skill_bundle_revision_retired"),
                (legacy, "agent_skill_bundle_revision_retired"),
                ("skillrev-1000000-f84bc49b3ad8", "agent_skill_bundle_revision_retired"),
                ("skillrev-forged", "agent_skill_bundle_revision_invalid"),
                (
                    "skillrev-v2-" + ("a" * 64),
                    "agent_skill_bundle_revision_unavailable",
                ),
            )
            for revision, expected_code in cases:
                with self.subTest(revision=revision):
                    with self.assertRaises(SkillBundleRevisionError) as raised:
                        state.bundle_for_revision(revision)
                    self.assertEqual(raised.exception.safe_error_code, expected_code)

            self.assertIs(state.bundle_for_revision(state.active_revision), state.active_bundle)

    def test_retain_revision_requires_explicit_v2(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "skill"
            self._write_skill(root, "demo-skill", "stable")
            state = SkillRuntimeState.from_roots(
                skill_roots=(root,),
                public_skill_roots=(root,),
                reserved_capability_ids=("main_agent.respond",),
            )

            with self.assertRaises(SkillBundleRevisionError) as raised:
                state.retain_revision(None)
            self.assertEqual(
                raised.exception.safe_error_code,
                "agent_skill_bundle_revision_retired",
            )

    def test_refresh_removes_deleted_public_skill_from_active_descriptors(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "skill"
            self._write_skill(root, "demo-skill", "version one")
            state = SkillRuntimeState.from_roots(
                skill_roots=(root,),
                public_skill_roots=(root,),
                reserved_capability_ids=("main_agent.respond",),
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
                reserved_capability_ids=("main_agent.respond",),
            )
            first_revision = state.active_revision

            with patch(
                "src.integrations.agent_skills.skill_runtime_state.SkillCatalog.from_roots",
                side_effect=RuntimeError("boom"),
            ):
                result = state.refresh_if_changed(reason="conversation_start", force=True)

            self.assertEqual(result.status, "failed")
            self.assertEqual(result.error_type, "RuntimeError")
            self.assertEqual(state.active_revision, first_revision)
            self.assertIn("skill.demo_skill", state.active_skill_capability_ids())

    def test_known_skill_capability_ids_keep_retained_revision_until_release(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "skill"
            self._write_skill(root, "demo-skill", "version one")
            state = SkillRuntimeState.from_roots(
                skill_roots=(root,),
                public_skill_roots=(root,),
                reserved_capability_ids=("main_agent.respond",),
            )
            first_revision = state.active_revision
            state.retain_revision(first_revision)

            (root / "demo-skill" / "SKILL.md").unlink()
            result = state.refresh_if_changed(reason="conversation_start")

            self.assertEqual(result.status, "completed")
            self.assertIn("skill.demo_skill", state.known_skill_capability_ids())
            state.release_revision(first_revision)
            self.assertNotIn("skill.demo_skill", state.known_skill_capability_ids())


if __name__ == "__main__":
    unittest.main()
