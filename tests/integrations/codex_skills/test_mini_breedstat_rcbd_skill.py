from __future__ import annotations

import asyncio
import importlib.util
import json
import shutil
import unittest
from pathlib import Path

from src.integrations.codex_skills import SkillCatalog, SkillScriptRunner, match_skills, parse_skill_file


class MiniBreedstatRcbdSkillCompatibilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.skill_file = Path("skill/mini_breedstat_rcbd_skill/SKILL.md")
        if not self.skill_file.exists():
            self.skipTest("local mini BreedStat RCBD skill is not present under ignored skill/")

    def _skip_without_rscript(self) -> None:
        candidates = (
            shutil.which("Rscript"),
            "/usr/local/bin/Rscript",
            "/opt/homebrew/bin/Rscript",
            "/Library/Frameworks/R.framework/Resources/bin/Rscript",
        )
        if not any(candidate and Path(candidate).exists() for candidate in candidates):
            self.skipTest("local Rscript runtime is not installed")

    def test_project_skill_catalog_discovers_rcbd_skill(self) -> None:
        manifest = parse_skill_file(self.skill_file)
        catalog = SkillCatalog.from_roots(["skill"])
        matches = match_skills("帮我做一个RCBD随机区组田间设计", catalog)

        self.assertEqual(manifest.name, "mini-breedstat-rcbd")
        self.assertEqual(manifest.outputs.required, ("answer",))
        self.assertEqual(len(manifest.scripts), 1)
        self.assertEqual(manifest.scripts[0].runtime, "python")
        self.assertTrue(manifest.scripts[0].auto_run)
        self.assertIn("mini-breedstat-rcbd", [skill.name for skill in catalog.skills])
        self.assertEqual(matches[0].manifest.name, "mini-breedstat-rcbd")

        trigger_queries = (
            "帮我用上传材料做随机区组，2次重复",
            "请生成随机区组设计 fieldbook",
            "make a randomized complete block design for these materials",
            "按对照位置约束做田间小区排布",
        )
        for query in trigger_queries:
            with self.subTest(query=query):
                query_matches = match_skills(query, catalog)
                self.assertGreater(query_matches[0].score, 0)
                self.assertEqual(query_matches[0].manifest.name, "mini-breedstat-rcbd")

    def test_wrapper_returns_json_answer_when_required_input_is_missing(self) -> None:
        manifest = parse_skill_file(self.skill_file)
        result = asyncio.run(
            SkillScriptRunner().run(
                manifest,
                manifest.scripts[0],
                {"query": "帮我做一个RCBD随机区组田间设计", "uploaded_artifacts": [], "metadata": {}},
            )
        )

        self.assertIn("answer", result)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["type"], "missing_input")
        self.assertIn("blocks", result["missing"])

    def test_wrapper_parse_blocks_accepts_chinese_repeat_classifier_and_top_level_override(self) -> None:
        script_file = Path("skill/mini_breedstat_rcbd_skill/scripts/run_rcbd.py")
        if not script_file.exists():
            self.skipTest("local mini BreedStat RCBD wrapper is not present")
        spec = importlib.util.spec_from_file_location("run_rcbd", script_file)
        module = importlib.util.module_from_spec(spec)
        assert spec is not None and spec.loader is not None
        spec.loader.exec_module(module)

        self.assertEqual(module.parse_blocks({"query": "要求2次重复"}, {}), 2)
        self.assertEqual(module.parse_blocks({"query": "要求2次重复", "blocks": 3}, {}), 3)

    def test_wrapper_uses_bundled_rcbd_core_dependency(self) -> None:
        self._skip_without_rscript()
        manifest = parse_skill_file(self.skill_file)
        sample_data = json.loads(Path("skill/mini_breedstat_rcbd_skill/examples/rcbd_sample.json").read_text())

        result = asyncio.run(
            SkillScriptRunner().run(
                manifest,
                manifest.scripts[0],
                {
                    "query": "请用3个区组做RCBD随机区组设计",
                    "uploaded_artifacts": [],
                    "metadata": {"input_data": sample_data},
                },
            )
        )

        self.assertTrue(result["ok"])
        self.assertIn("RCBD 设计已完成", result["answer"])
        self.assertEqual(result["design"], "rcbd")
        self.assertEqual(len(result["out_design"]), 30)
        if result.get("layout_html_generated"):
            self.assertEqual(result["output_files"][0]["path"], "outputs/rcbd_layout.html")
            self.assertNotIn("layout_html", result)

    def test_wrapper_accepts_uploaded_artifact_content(self) -> None:
        self._skip_without_rscript()
        manifest = parse_skill_file(self.skill_file)
        csv_content = Path("skill/mini_breedstat_rcbd_skill/examples/rcbd_sample_plot_hyb_set.csv").read_text()

        result = asyncio.run(
            SkillScriptRunner().run(
                manifest,
                manifest.scripts[0],
                {
                    "query": "请用3个区组做RCBD随机区组设计",
                    "uploaded_artifacts": [{"filename": "materials.csv", "content": csv_content}],
                    "metadata": {},
                },
            )
        )

        self.assertTrue(result["ok"])
        self.assertIn("RCBD 设计已完成", result["answer"])
        self.assertEqual(result["design"], "rcbd")
        if result.get("layout_html_generated"):
            self.assertEqual(result["output_files"][0]["mime_type"], "text/html")
            self.assertNotIn("layout_html", result)


if __name__ == "__main__":
    unittest.main()
