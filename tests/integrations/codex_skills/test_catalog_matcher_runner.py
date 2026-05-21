from __future__ import annotations

import asyncio
import tempfile
import textwrap
import unittest
from pathlib import Path

from src.integrations.codex_skills import SkillCatalog, SkillScriptError, SkillScriptRunner, match_skills, parse_skill_file


class SkillCatalogMatcherRunnerTest(unittest.TestCase):
    def test_catalog_discovers_skills_and_matcher_scores_triggers(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_dir = Path(tmpdir) / "report"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                """---
name: report-writer
description: 生成汇报材料
triggers:
  - 周报
---

# Body
生成结构化周报。
""",
                encoding="utf-8",
            )

            catalog = SkillCatalog.from_roots([tmpdir])
            matches = match_skills("帮我写本周周报", catalog)

        self.assertEqual([skill.name for skill in catalog.skills], ["report-writer"])
        self.assertEqual(matches[0].manifest.name, "report-writer")
        self.assertGreater(matches[0].score, 0)
        self.assertIn("trigger", matches[0].reason)

    def test_matcher_defaults_to_single_best_skill_for_progressive_disclosure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            for name, trigger in (
                ("first-report", "周报"),
                ("second-report", "周报"),
            ):
                skill_dir = root / name
                skill_dir.mkdir()
                (skill_dir / "SKILL.md").write_text(
                    f"""---
name: {name}
description: 生成汇报材料
triggers:
  - {trigger}
---

# {name}
""",
                    encoding="utf-8",
                )

            catalog = SkillCatalog.from_roots([root])

        default_matches = match_skills("帮我写本周周报", catalog)
        expanded_matches = match_skills("帮我写本周周报", catalog, max_matches=2)

        self.assertEqual(len(default_matches), 1)
        self.assertEqual([match.manifest.name for match in expanded_matches], ["first-report", "second-report"])

    def test_runner_executes_declared_python_script_with_json_io(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_dir = Path(tmpdir) / "scripted"
            scripts_dir = skill_dir / "scripts"
            scripts_dir.mkdir(parents=True)
            (scripts_dir / "echo.py").write_text(
                textwrap.dedent(
                    """
                    import json
                    import sys

                    payload = json.load(sys.stdin)
                    print(json.dumps({"answer": "processed " + payload["query"]}, ensure_ascii=False))
                    """
                ).strip(),
                encoding="utf-8",
            )
            skill_file = skill_dir / "SKILL.md"
            skill_file.write_text(
                """---
name: scripted
scripts:
  - name: echo
    path: scripts/echo.py
    auto_run: true
outputs:
  required:
    - answer
---

# Scripted
Run script.
""",
                encoding="utf-8",
            )
            manifest = parse_skill_file(skill_file)

            result = asyncio.run(SkillScriptRunner().run(manifest, manifest.scripts[0], {"query": "hello"}))

        self.assertEqual(result, {"answer": "processed hello"})

    def test_runner_rejects_path_escape(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_file = Path(tmpdir) / "SKILL.md"
            skill_file.write_text(
                """---
name: escape
scripts:
  - name: bad
    path: ../evil.py
    auto_run: true
---

# Escape
Bad script.
""",
                encoding="utf-8",
            )
            manifest = parse_skill_file(skill_file)

            with self.assertRaises(SkillScriptError):
                asyncio.run(SkillScriptRunner().run(manifest, manifest.scripts[0], {"query": "x"}))


if __name__ == "__main__":
    unittest.main()
