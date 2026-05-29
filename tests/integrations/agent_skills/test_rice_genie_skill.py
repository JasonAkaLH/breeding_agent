from __future__ import annotations

import asyncio
import unittest
from pathlib import Path

from src.integrations.agent_skills import SkillCatalog, SkillScriptRunner, match_skills, parse_skill_file
from src.integrations.agent_skills.output_files import collect_skill_output_files


SAMPLE_VCF = """##fileformat=VCFv4.2
#CHROM	POS	ID	REF	ALT	QUAL	FILTER	INFO	FORMAT	S1
Chr1	5271719	.	TTCAGCCATGGG	T	.	PASS	.	GT	1/1
Chr1	5270928	.	A	C	.	PASS	.	GT	1/1
Chr1	5568692	.	T	C	.	PASS	.	GT	0/0
"""


class RiceGenieSkillCompatibilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.skill_file = Path("skill/rice-genie/SKILL.md")
        if not self.skill_file.exists():
            self.skipTest("rice-genie skill is not present")

    def test_manifest_declares_public_python_subprocess_contract(self) -> None:
        manifest = parse_skill_file(self.skill_file)

        self.assertEqual(manifest.name, "rice-genie")
        self.assertEqual(manifest.metadata.get("capability_id"), "skill.rice_genie")
        self.assertEqual(manifest.metadata.get("execution", {}).get("mode"), "python_subprocess")
        self.assertEqual(manifest.metadata.get("execution", {}).get("answer_mode"), "requires_finalizer")
        self.assertGreaterEqual(len(manifest.triggers), 8)
        self.assertEqual(manifest.outputs.required, ("answer",))
        self.assertIn("rice_input", manifest.parameters)
        self.assertTrue(manifest.parameters["rice_input"].required)
        self.assertEqual(manifest.parameters["rice_input"].type, "artifact")
        self.assertIn("sample", manifest.parameters)
        self.assertIn("samples", manifest.parameters)
        self.assertEqual(len(manifest.scripts), 1)
        self.assertEqual(manifest.scripts[0].path, "scripts/run_rice_genie.py")
        self.assertEqual(manifest.scripts[0].runtime, "python")
        self.assertTrue(manifest.scripts[0].auto_run)

    def test_project_catalog_matches_rice_genie_queries(self) -> None:
        catalog = SkillCatalog.from_roots(["skill"])

        trigger_queries = (
            "请做水稻基因型体检",
            "水稻VCF 帮我生成体检报告",
            "run rice qtn gene check",
            "统计优良变异并解读",
        )
        for query in trigger_queries:
            with self.subTest(query=query):
                matches = match_skills(query, catalog, max_matches=3)
                self.assertGreater(matches[0].score, 0)
                self.assertEqual(matches[0].manifest.name, "rice-genie")

    def test_wrapper_returns_json_answer_when_required_input_missing(self) -> None:
        manifest = parse_skill_file(self.skill_file)
        result = asyncio.run(
            SkillScriptRunner().run(
                manifest,
                manifest.scripts[0],
                {"query": "请做水稻体检", "uploaded_artifacts": [], "metadata": {}},
            )
        )

        self.assertFalse(result["ok"])
        self.assertIs(result["is_error"], True)
        self.assertIn("answer", result)
        self.assertEqual(result["error"]["type"], "missing_input")
        self.assertIn("rice_input", result["missing"])

    def test_wrapper_runs_vcf_matching_and_declares_markdown_report_output(self) -> None:
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
                    "query": "请做水稻基因型体检",
                    "run_id": "unit_demo",
                    "uploaded_artifacts": [{"filename": "sample.vcf", "content": SAMPLE_VCF}],
                    "metadata": {},
                },
            )
        )

        self.assertTrue(result["ok"])
        self.assertIn("## 水稻基因型体检报告", result["answer"])
        self.assertEqual(result["report_format"], "rice-genie-key-trait-report-v1")
        self.assertEqual(len(result["output_files"]), 1)
        self.assertEqual(result["output_files"][0]["mime_type"], "text/markdown")
        self.assertEqual(len(captured["files"]), 1)
        self.assertEqual(captured["rejections"], ())
