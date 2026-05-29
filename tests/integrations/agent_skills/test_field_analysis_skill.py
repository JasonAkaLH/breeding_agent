from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import shutil
import unittest
from unittest import mock
from pathlib import Path

from src.integrations.agent_skills import SkillCatalog, SkillScriptRunner, match_skills, parse_skill_file
from src.integrations.agent_skills.output_files import collect_skill_output_files


SAMPLE_CSV = """loc_id,rep_num,entry_id,ped_id,trait,value,check_type,ranges,pass,value_trend
L1,1,E1,P1,T001,10,test,1,1,1
L1,1,E2,P2,T001,12,test,1,2,1
L1,1,E3,CK1,T001,11,check,1,3,1
L1,2,E1,P1,T001,11,test,2,1,1
L1,2,E2,P2,T001,13,test,2,2,1
L1,2,E3,CK1,T001,10,check,2,3,1
L2,1,E1,P1,T001,9,test,1,1,1
L2,1,E2,P2,T001,14,test,1,2,1
L2,1,E3,CK1,T001,10,check,1,3,1
L2,2,E1,P1,T001,10,test,2,1,1
L2,2,E2,P2,T001,15,test,2,2,1
L2,2,E3,CK1,T001,11,check,2,3,1
"""


class FieldAnalysisSkillCompatibilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.skill_file = Path("skill/field-analysis/SKILL.md")
        if not self.skill_file.exists():
            self.skipTest("field-analysis skill is not present")

    def test_backend_dockerfile_installs_r_runtime_requirements(self) -> None:
        dockerfile = Path("Dockerfile").read_text(encoding="utf-8")

        self.assertIn("LANG=C.UTF-8", dockerfile)
        self.assertIn("LC_ALL=C.UTF-8", dockerfile)
        self.assertIn("locales", dockerfile)
        self.assertIn("r-base-core", dockerfile)
        self.assertIn("r-cran-jsonlite", dockerfile)
        self.assertIn("R-backed Skill bundles require UTF-8 source parsing and jsonlite JSON output.", dockerfile)

    def test_wrapper_builds_utf8_rscript_environment(self) -> None:
        module_path = Path("skill/field-analysis/scripts/run_field_analysis.py")
        spec = importlib.util.spec_from_file_location("field_analysis_wrapper", module_path)
        if spec is None or spec.loader is None:
            self.fail("Unable to load field-analysis wrapper")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        with mock.patch.dict(os.environ, {"LANG": "C", "LC_ALL": "POSIX", "LC_CTYPE": "C"}, clear=False):
            env = module._rscript_env()

        self.assertEqual(env["LANG"], "C.UTF-8")
        self.assertEqual(env["LC_ALL"], "C.UTF-8")
        self.assertEqual(env["LC_CTYPE"], "C.UTF-8")
        self.assertEqual(env["PATH"], "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin")

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

        self.assertEqual(manifest.name, "field-analysis")
        self.assertEqual(manifest.metadata.get("capability_id"), "skill.field_analysis")
        self.assertEqual(manifest.metadata.get("execution", {}).get("mode"), "python_subprocess")
        self.assertEqual(manifest.metadata.get("execution", {}).get("answer_mode"), "requires_finalizer")
        self.assertEqual(manifest.outputs.required, ("answer",))
        self.assertIn("field_data", manifest.parameters)
        self.assertTrue(manifest.parameters["field_data"].required)
        self.assertEqual(manifest.parameters["field_data"].type, "artifact")
        self.assertIn("design", manifest.parameters)
        self.assertTrue(manifest.parameters["design"].required)
        self.assertEqual(len(manifest.scripts), 1)
        self.assertEqual(manifest.scripts[0].path, "scripts/run_field_analysis.py")
        self.assertEqual(manifest.scripts[0].runtime, "python")
        self.assertTrue(manifest.scripts[0].auto_run)

    def test_project_catalog_matches_field_analysis_queries(self) -> None:
        catalog = SkillCatalog.from_roots(["skill"])

        trigger_queries = (
            "请做田间数据分析，设计类型 rcbd",
            "帮我分析随机区组数据并做 LSD分组",
            "对角线增广分析这个田间表型数据",
            "run field trial analysis for this phenotype file",
        )
        for query in trigger_queries:
            with self.subTest(query=query):
                matches = match_skills(query, catalog, max_matches=3)
                self.assertGreater(matches[0].score, 0)
                self.assertEqual(matches[0].manifest.name, "field-analysis")

    def test_wrapper_returns_json_answer_when_required_inputs_missing(self) -> None:
        manifest = parse_skill_file(self.skill_file)
        result = asyncio.run(
            SkillScriptRunner().run(
                manifest,
                manifest.scripts[0],
                {"query": "请做田间数据分析", "uploaded_artifacts": [], "metadata": {}},
            )
        )

        self.assertFalse(result["ok"])
        self.assertIs(result["is_error"], True)
        self.assertIn("answer", result)
        self.assertEqual(result["error"]["type"], "missing_input")
        self.assertIn("design", result["missing"])
        self.assertIn("field_data", result["missing"])

    def test_wrapper_calls_bundled_r_runner_and_declares_safe_json_outputs(self) -> None:
        self._skip_without_rscript()
        manifest = parse_skill_file(self.skill_file)
        captured: dict[str, object] = {}

        async def processor(*, output, outputs_dir, manifest, script, context):
            captured["outputs_dir"] = outputs_dir
            collection = collect_skill_output_files(output, outputs_dir, manifest=manifest)
            captured["files"] = collection.files
            captured["rejections"] = collection.rejections
            return output

        result = asyncio.run(
            SkillScriptRunner(output_processor=processor).run(
                manifest,
                manifest.scripts[0],
                {
                    "query": "请分析这个 RCBD 田间表型数据",
                    "design": "rcbd",
                    "run_id": "unit_demo",
                    "uploaded_artifacts": [{"filename": "field.csv", "content": SAMPLE_CSV}],
                    "metadata": {},
                },
            )
        )

        self.assertTrue(result["ok"])
        self.assertIn("田间数据分析已完成", result["answer"])
        self.assertEqual(result["design"], "rcbd")
        self.assertEqual(result["format"], "field-analysis-report-v1")
        self.assertIn("T001", result["available_traits"])
        self.assertEqual(len(result["output_files"]), 2)
        self.assertEqual([item["mime_type"] for item in result["output_files"]], ["application/json", "application/json"])
        self.assertEqual(len(captured["files"]), 2)
        self.assertEqual(captured["rejections"], ())
