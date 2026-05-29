from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.integrations.agent_skills import SkillParseError, parse_skill_file


class AgentSkillParserTest(unittest.TestCase):
    def test_parse_agent_skill_manifest_with_project_extensions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_file = Path(tmpdir) / "SKILL.md"
            skill_file.write_text(
                """---
name: report-writer
description: 生成周报和汇报材料
triggers:
  - 周报
inputs:
  required:
    - query
outputs:
  required:
    - answer
scripts:
  - name: render
    path: scripts/render.py
    runtime: python
    auto_run: true
    timeout_seconds: 3
parameters:
  blocks:
    type: integer
    required: true
    source: query
    aliases:
      - 重复
      - 区组
    patterns:
      - '(\\d+)\\s*(?:个|次)?(?:重复|区组)'
custom_field: keep-me
---

# Report Writer

请按管理汇报风格输出。
""",
                encoding="utf-8",
            )

            manifest = parse_skill_file(skill_file)

        self.assertEqual(manifest.name, "report-writer")
        self.assertEqual(manifest.description, "生成周报和汇报材料")
        self.assertEqual(manifest.triggers, ("周报",))
        self.assertIn("管理汇报风格", manifest.body)
        self.assertEqual(manifest.inputs.required, ("query",))
        self.assertEqual(manifest.outputs.required, ("answer",))
        self.assertEqual(len(manifest.scripts), 1)
        self.assertEqual(manifest.scripts[0].name, "render")
        self.assertEqual(manifest.scripts[0].path, "scripts/render.py")
        self.assertTrue(manifest.scripts[0].auto_run)
        self.assertEqual(manifest.scripts[0].timeout_seconds, 3)
        self.assertEqual(manifest.parameters["blocks"].type, "integer")
        self.assertTrue(manifest.parameters["blocks"].required)
        self.assertEqual(manifest.parameters["blocks"].sources, ("query",))
        self.assertEqual(manifest.parameters["blocks"].aliases, ("重复", "区组"))
        self.assertEqual(manifest.parameters["blocks"].patterns, (r"(\d+)\s*(?:个|次)?(?:重复|区组)",))
        self.assertEqual(manifest.metadata["custom_field"], "keep-me")

    def test_rejects_invalid_or_empty_skill(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_file = Path(tmpdir) / "SKILL.md"
            skill_file.write_text("---\nname: broken\n---\n\n", encoding="utf-8")

            with self.assertRaises(SkillParseError):
                parse_skill_file(skill_file)


if __name__ == "__main__":
    unittest.main()
