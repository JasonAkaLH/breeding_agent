from __future__ import annotations

import unittest
from pathlib import Path

from src.integrations.codex_skills import parse_skill_file


class ProjectSkillManifestContractTest(unittest.TestCase):
    def test_all_project_skills_declare_display_name(self) -> None:
        skill_files = sorted(Path("skill").glob("*/SKILL.md"))
        self.assertGreater(len(skill_files), 0)
        missing: list[str] = []
        for skill_file in skill_files:
            manifest = parse_skill_file(skill_file)
            display_name = manifest.metadata.get("display_name")
            if not isinstance(display_name, str) or not display_name.strip():
                missing.append(str(skill_file))
        self.assertEqual(missing, [])
