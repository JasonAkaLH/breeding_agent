from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.integrations.codex_skills import SkillCatalog
from src.integrations.codex_skills.skill_capabilities import build_skill_capability_registry


class SkillCapabilityMappingTest(unittest.TestCase):
    def _write_skill(self, root: Path, dirname: str, *, name: str, description: str = "生成设计", metadata: str = "") -> Path:
        skill_dir = root / dirname
        skill_dir.mkdir(parents=True)
        metadata_block = f"metadata:\n{metadata}" if metadata else ""
        (skill_dir / "SKILL.md").write_text(
            f"""---
name: {name}
description: {description}
triggers:
  - 随机区组
{metadata_block}
---

# {name}
""",
            encoding="utf-8",
        )
        return skill_dir / "SKILL.md"

    def test_project_skill_becomes_public_skill_capability(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir) / "skill"
            self._write_skill(
                project_root,
                "rcbd",
                name="mini-breedstat-rcbd",
                description="生成 RCBD 随机区组设计",
                metadata="  display_name: 田间试验设计\n",
            )
            catalog = SkillCatalog.from_roots([project_root])

            registry = build_skill_capability_registry(catalog, public_skill_roots=(project_root,))

        self.assertEqual(tuple(registry.descriptors_by_id), ("skill.mini_breedstat_rcbd",))
        descriptor = registry.descriptors_by_id["skill.mini_breedstat_rcbd"]
        self.assertEqual(descriptor.name, "mini-breedstat-rcbd")
        self.assertEqual(descriptor.display_name, "田间试验设计")
        self.assertEqual(descriptor.kind, "skill")
        self.assertEqual(descriptor.source, "skill")
        self.assertEqual(descriptor.source_path, "rcbd/SKILL.md")
        self.assertTrue(descriptor.public)
        self.assertEqual(registry.skill_name_by_capability_id["skill.mini_breedstat_rcbd"], "mini-breedstat-rcbd")

    def test_user_skill_outside_public_roots_is_not_registered(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir) / "skill"
            user_root = Path(tmpdir) / "user-skills"
            self._write_skill(project_root, "public", name="public-skill")
            self._write_skill(user_root, "private", name="private-skill")
            catalog = SkillCatalog.from_roots([project_root, user_root])

            registry = build_skill_capability_registry(catalog, public_skill_roots=(project_root,))

        self.assertEqual(set(registry.descriptors_by_id), {"skill.public_skill"})
        skipped = {diagnostic.skill_name: diagnostic.reason for diagnostic in registry.diagnostics}
        self.assertEqual(skipped["private-skill"], "not_public_scope")

    def test_invalid_and_duplicate_capability_ids_are_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir) / "skill"
            self._write_skill(project_root, "invalid", name="bad", metadata="  capability_id: main_agent.respond\n")
            self._write_skill(project_root, "first", name="same name")
            self._write_skill(project_root, "second", name="same_name")
            catalog = SkillCatalog.from_roots([project_root])

            registry = build_skill_capability_registry(catalog, public_skill_roots=(project_root,))

        self.assertEqual(set(registry.descriptors_by_id), set())
        reasons = sorted(diagnostic.reason for diagnostic in registry.diagnostics)
        self.assertEqual(reasons, ["duplicate", "duplicate", "invalid_id"])

    def test_unsupported_script_runtime_is_not_public_capability(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir) / "skill"
            skill_dir = project_root / "shell"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                """---
name: shell-helper
description: 不应公开的 shell runtime
scripts:
  - name: run
    path: run.sh
    runtime: shell
    auto_run: true
---

# Shell
""",
                encoding="utf-8",
            )
            catalog = SkillCatalog.from_roots([project_root])

            registry = build_skill_capability_registry(catalog, public_skill_roots=(project_root,))

        self.assertEqual(set(registry.descriptors_by_id), set())
        self.assertEqual(len(registry.diagnostics), 1)
        self.assertEqual(registry.diagnostics[0].skill_name, "shell-helper")
        self.assertEqual(registry.diagnostics[0].reason, "unsupported_runtime")


if __name__ == "__main__":
    unittest.main()
