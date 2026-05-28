from __future__ import annotations

import asyncio
import shutil
import unittest
from pathlib import Path

from src.integrations.codex_skills import SkillCatalog, SkillScriptRunner, match_skills, parse_skill_file
from src.integrations.codex_skills.output_files import collect_skill_output_files


RCBD_SAMPLE_CSV = """ped_id,hyb_check,set
P1,0,A
P2,0,A
P3,0,A
CK1,1,A
P4,0,B
P5,0,B
P6,0,B
CK2,1,B
"""

INTERVAL_SAMPLE_CSV = """ped_id,hyb_check,set
P1,0,A
P2,0,A
CK1,1,A
P3,0,A
CK2,1,A
"""


class FieldDesignSkillCompatibilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.skill_file = Path("skill/field-design/SKILL.md")
        if not self.skill_file.exists():
            self.skipTest("field-design skill is not present")

    def _skip_without_rscript(self) -> None:
        candidates = (
            shutil.which("Rscript"),
            "/usr/local/bin/Rscript",
            "/opt/homebrew/bin/Rscript",
            "/Library/Frameworks/R.framework/Resources/bin/Rscript",
            "/usr/bin/Rscript",
        )
        if not any(candidate and Path(candidate).exists() for candidate in candidates):
            self.skipTest("Rscript runtime is not installed")

    def test_manifest_declares_public_python_subprocess_contract(self) -> None:
        manifest = parse_skill_file(self.skill_file)

        self.assertEqual(manifest.name, "field-design")
        self.assertEqual(manifest.metadata.get("capability_id"), "skill.field_design")
        self.assertEqual(manifest.metadata.get("execution", {}).get("mode"), "python_subprocess")
        self.assertEqual(manifest.metadata.get("execution", {}).get("answer_mode"), "requires_finalizer")
        self.assertGreaterEqual(len(manifest.triggers), 8)
        self.assertEqual(manifest.outputs.required, ("answer",))
        self.assertIn("material_data", manifest.parameters)
        self.assertTrue(manifest.parameters["material_data"].required)
        self.assertEqual(manifest.parameters["material_data"].type, "artifact")
        self.assertIn("design", manifest.parameters)
        self.assertTrue(manifest.parameters["design"].required)
        self.assertIn("blocks", manifest.parameters)
        self.assertIn("ncols", manifest.parameters)
        self.assertIn("ck_spec", manifest.parameters)
        self.assertEqual(len(manifest.scripts), 1)
        self.assertEqual(manifest.scripts[0].path, "scripts/run_field_design.py")
        self.assertEqual(manifest.scripts[0].runtime, "python")
        self.assertTrue(manifest.scripts[0].auto_run)

    def test_project_catalog_matches_field_design_queries(self) -> None:
        catalog = SkillCatalog.from_roots(["skill"])

        trigger_queries = (
            "请做田间试验设计",
            "我要做对角线增广设计，ncols 20",
            "帮我生成 interval contrast design",
            "请生成fieldbook和田间布局预览",
        )
        for query in trigger_queries:
            with self.subTest(query=query):
                matches = match_skills(query, catalog, max_matches=3)
                self.assertGreater(matches[0].score, 0)
                self.assertEqual(matches[0].manifest.name, "field-design")

    def test_wrapper_returns_json_answer_when_required_inputs_missing(self) -> None:
        manifest = parse_skill_file(self.skill_file)
        result = asyncio.run(
            SkillScriptRunner().run(
                manifest,
                manifest.scripts[0],
                {"query": "请做田间试验设计", "uploaded_artifacts": [], "metadata": {}},
            )
        )

        self.assertFalse(result["ok"])
        self.assertIs(result["is_error"], True)
        self.assertIn("answer", result)
        self.assertEqual(result["error"]["type"], "missing_input")
        self.assertIn("material_data", result["missing"])
        self.assertIn("design", result["missing"])

    def test_wrapper_calls_bundled_rcbd_pipeline_and_declares_csv_html_outputs(self) -> None:
        self._skip_without_rscript()
        manifest = parse_skill_file(self.skill_file)
        captured: dict[str, object] = {}

        async def processor(*, output, outputs_dir, manifest, script, context):
            collection = collect_skill_output_files(output, outputs_dir, manifest=manifest)
            captured["files"] = collection.files
            captured["rejections"] = collection.rejections
            return output

        result = asyncio.run(
            SkillScriptRunner(output_processor=processor).run(
                manifest,
                manifest.scripts[0],
                {
                    "query": "请用 2 次重复做 RCBD 随机区组设计",
                    "design": "rcbd",
                    "blocks": 2,
                    "run_id": "unit_demo",
                    "uploaded_artifacts": [{"filename": "materials.csv", "content": RCBD_SAMPLE_CSV}],
                    "metadata": {},
                },
            )
        )

        self.assertTrue(result["ok"])
        self.assertIn("RCBD 试验设计已完成", result["answer"])
        self.assertEqual(result["design"], "rcbd")
        self.assertEqual(result["columns"], ["plots", "r", "ped_id", "ranges", "pass", "set", "hyb_check", "hyb_type"])
        self.assertEqual(len(result["rows"]), 10)
        self.assertEqual([item["mime_type"] for item in result["output_files"]], ["text/csv", "text/html"])
        self.assertEqual(len(captured["files"]), 2)
        self.assertEqual(captured["rejections"], ())

    def test_interval_without_ck_spec_returns_ck_table_prompt(self) -> None:
        self._skip_without_rscript()
        manifest = parse_skill_file(self.skill_file)
        result = asyncio.run(
            SkillScriptRunner().run(
                manifest,
                manifest.scripts[0],
                {
                    "query": "请做间比法设计，ncols 10",
                    "design": "interval",
                    "ncols": 10,
                    "uploaded_artifacts": [{"filename": "materials.csv", "content": INTERVAL_SAMPLE_CSV}],
                    "metadata": {},
                },
            )
        )

        self.assertFalse(result["ok"])
        self.assertIs(result["is_error"], True)
        self.assertEqual(result["error"]["type"], "missing_input")
        self.assertEqual(result["status"], "needs_ck_parameters")
        self.assertEqual(result["design"], "interval")
        self.assertIn("ck_spec", result["missing"])
        self.assertEqual(result["columns"], ["ck_no", "ped_id", "set"])
        self.assertGreaterEqual(len(result["rows"]), 2)
