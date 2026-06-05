from __future__ import annotations

import asyncio
import importlib.util
import shutil
import unittest
from pathlib import Path

from src.integrations.agent_skills import SkillCatalog, SkillScriptRunner, match_skills, parse_skill_file
from src.integrations.agent_skills.output_files import collect_skill_output_files


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
        self.assertIsNotNone(manifest.contract)
        contract = manifest.contract
        self.assertEqual(contract.capability.id, "skill.field_design")
        self.assertEqual(contract.runtime.mode, "python_subprocess")
        self.assertEqual(contract.runtime.answer_mode, "requires_finalizer")
        self.assertIn("rcbd", contract.input_schemas)
        self.assertIn("interval", contract.input_schemas)
        self.assertEqual(len(manifest.scripts), 1)
        self.assertEqual(manifest.scripts[0].path, "scripts/run_field_design.py")
        self.assertTrue(manifest.scripts[0].auto_run)

    def test_public_usage_keeps_material_header_as_ped_id(self) -> None:
        text = (self.skill_file.parent / "references" / "material-data.md").read_text(encoding="utf-8")
        self.assertIn("ped_id", text)
        self.assertNotIn("variety_name", text)

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

    def test_wrapper_positive_integer_parser_accepts_chinese_phrases(self) -> None:
        script_path = Path("skill/field-design/scripts/run_field_design.py")
        spec = importlib.util.spec_from_file_location("field_design_run_field_design_test", script_path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        self.assertEqual(module.get_positive_int({"blocks": "十个重复"}, "blocks"), 10)
        self.assertEqual(module.get_positive_int({"blocks": "两次"}, "blocks"), 2)
        self.assertEqual(module.get_positive_int({"blocks": "壹佰零贰"}, "blocks"), 102)
        self.assertEqual(
            module.get_positive_int(
                {"query": "请做随机区组，重复十次"},
                "blocks",
                (
                    r"(?:blocks?|区组数|区组|重复数|重复|reps?|replications?)\s*[:：=]?\s*(\d+)",
                    r"(\d+)\s*(?:个|次)?(?:区组|重复|rep|reps|blocks?)",
                ),
            ),
            10,
        )
        for invalid in (False, True, "0", "零", "-1", "1.5", "", "没有重复"):
            with self.subTest(invalid=invalid):
                self.assertIsNone(module.get_positive_int({"blocks": invalid}, "blocks"))

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
        self.assertEqual(result["columns"], ["小区编号", "区组", "材料编号", "行号", "列号", "组别", "对照标记", "材料类型"])
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
        self.assertEqual(result["columns"], ["CK编号", "材料编号", "组别"])
        self.assertGreaterEqual(len(result["rows"]), 2)
