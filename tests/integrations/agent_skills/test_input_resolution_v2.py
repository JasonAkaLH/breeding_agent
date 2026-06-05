from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from src.integrations.agent_skills import SkillCatalog, SkillScriptExecutionService, SkillScriptRunner


class _NoRunRunner(SkillScriptRunner):
    async def run(self, *_args, **_kwargs):  # pragma: no cover
        raise AssertionError("runner should not be called when missing")


class InputResolutionV2Test(unittest.TestCase):
    def test_selected_schema_required_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            skill = root / "field-design"
            (skill / "scripts").mkdir(parents=True)
            (skill / "schemas").mkdir()
            (skill / "SKILL.md").write_text("---\nname: field-design\ndescription: design\n---\n\n# Field\n", encoding="utf-8")
            (skill / "skill.contract.yaml").write_text("""
contract_version: '2'
capability: {id: skill.field_design, display_name: Field Design}
runtime: {mode: python_subprocess, answer_mode: direct}
schema_selector: {strategy: deterministic_then_llm, selector_field: design}
entrypoints: {run: {path: scripts/run.py}}
input_schemas:
  rcbd: {path: schemas/rcbd.input.yaml, aliases: [rcbd, 随机区组], entrypoint: run}
  interval: {path: schemas/interval.input.yaml, aliases: [interval, 间比法], entrypoint: run}
""", encoding="utf-8")
            (skill / "schemas" / "rcbd.input.yaml").write_text("""
schema_id: rcbd
inputs:
  design: {type: string, required: true, const: rcbd}
  ncols: {type: integer, required: true, aliases: [列数]}
""", encoding="utf-8")
            (skill / "schemas" / "interval.input.yaml").write_text("""
schema_id: interval
inputs:
  design: {type: string, required: true, const: interval}
  ncols: {type: integer, required: true, aliases: [列数]}
  ck_spec: {type: string, required: true, aliases: [ck_spec]}
""", encoding="utf-8")
            manifest = SkillCatalog.from_roots((root,)).get("field-design")
            assert manifest is not None
            script = next(iter(manifest.contract.entrypoints.values()))
            from src.integrations.agent_skills import SkillScriptEntrypoint
            entry = SkillScriptEntrypoint(name=script.name, path=script.path, auto_run=True)
            service = SkillScriptExecutionService(script_runner=_NoRunRunner())
            rcbd = asyncio.run(service.execute(manifest=manifest, script=entry, user_message="随机区组 列数 10", metadata={}, artifact_context=(), output_context={}))
            interval = asyncio.run(service.execute(manifest=manifest, script=entry, user_message="间比法 列数 10", metadata={}, artifact_context=(), output_context={}))

        self.assertNotIn("ck_spec", rcbd.missing)
        self.assertIn("ck_spec", interval.missing)
        self.assertEqual(interval.resolution.payload["_selected_schema_id"], "interval")

