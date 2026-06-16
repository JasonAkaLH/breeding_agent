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
        text = (self.skill_file.parent / "references" / "material-format.md").read_text(encoding="utf-8")
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
