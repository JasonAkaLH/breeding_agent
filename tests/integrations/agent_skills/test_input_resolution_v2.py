from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from src.integrations.agent_skills import SkillCatalog, SkillScriptExecutionService, SkillScriptRunner


class _NoRunRunner(SkillScriptRunner):
    async def run(self, *_args, **_kwargs):  # pragma: no cover
        raise AssertionError("runner should not be called when missing")


class _CaptureRunner(SkillScriptRunner):
    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    async def run(self, _manifest, _script, payload, *, output_context):
        del output_context
        self.payloads.append(dict(payload))
        return {"answer": "ok"}


class InputResolutionV2Test(unittest.TestCase):
    def _write_field_design_skill(self, root: Path) -> Any:
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
  material_data: {type: artifact, required: true, source: {allowed: [artifact]}, aliases: [材料清单]}
  ncols: {type: integer, required: true, aliases: [列数, 田块列数]}
  blocks: {type: integer, required: false, aliases: [重复, 区组数]}
""", encoding="utf-8")
        (skill / "schemas" / "interval.input.yaml").write_text("""
schema_id: interval
inputs:
  design: {type: string, required: true, const: interval}
  material_data: {type: artifact, required: true, source: {allowed: [artifact]}, aliases: [材料清单]}
  ncols: {type: integer, required: true, aliases: [列数, 田块列数]}
  blocks: {type: integer, required: false, aliases: [重复, 区组数]}
  ck_spec: {type: string, required: true, aliases: [ck_spec, CK参数, CK间隔]}
""", encoding="utf-8")
        manifest = SkillCatalog.from_roots((root,)).get("field-design")
        assert manifest is not None
        return manifest

    def _entrypoint(self, manifest):
        script = next(iter(manifest.contract.entrypoints.values()))
        from src.integrations.agent_skills import SkillScriptEntrypoint
        return SkillScriptEntrypoint(name=script.name, path=script.path, auto_run=True)

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

    def test_v2_initial_llm_resolves_required_and_optional_text_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            manifest = self._write_field_design_skill(root)
            runner = _CaptureRunner()
            prompts: list[str] = []

            async def slot_generator(prompt: str, **_kwargs) -> str:
                prompts.append(prompt)
                return json.dumps(
                    {
                        "resolved": {
                            "ncols": {"raw_value": "田块10列", "value": 10, "source": "query"},
                            "ck_spec": {"raw_value": "ck：1,2,8; 2,6,11", "value": "1,2,8; 2,6,11", "source": "query"},
                            "blocks": {"raw_value": "3个重复", "value": 3, "source": "query"},
                        }
                    },
                    ensure_ascii=False,
                )

            service = SkillScriptExecutionService(script_runner=runner, skill_input_text_generator=slot_generator)
            result = asyncio.run(
                service.execute(
                    manifest=manifest,
                    script=self._entrypoint(manifest),
                    user_message="做间比法，田块10列，3个重复，ck：1,2,8; 2,6,11",
                    metadata={},
                    artifact_context=({"filename": "materials.csv"},),
                    output_context={},
                )
            )

        self.assertEqual(result.status, "completed")
        self.assertEqual(runner.payloads[0]["ncols"], 10)
        self.assertEqual(runner.payloads[0]["ck_spec"], "1,2,8; 2,6,11")
        self.assertEqual(runner.payloads[0]["blocks"], 3)
        self.assertEqual(result.resolution.sources["ncols"].source, "llm_slot_resolver:query")
        self.assertEqual(result.resolution.sources["ck_spec"].source, "llm_slot_resolver:query")
        self.assertEqual(result.resolution.sources["blocks"].source, "llm_slot_resolver:query")
        self.assertIn("blocks", prompts[0])

    def test_v2_initial_llm_does_not_override_structured_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            manifest = self._write_field_design_skill(root)
            runner = _CaptureRunner()
            prompts: list[str] = []

            async def slot_generator(prompt: str, **_kwargs) -> str:
                prompts.append(prompt)
                return json.dumps(
                    {
                        "resolved": {
                            "ncols": {"raw_value": "田块10列", "value": 10, "source": "query"},
                            "ck_spec": {"raw_value": "CK参数=1,2,8", "value": "1,2,8", "source": "query"},
                        }
                    },
                    ensure_ascii=False,
                )

            service = SkillScriptExecutionService(script_runner=runner, skill_input_text_generator=slot_generator)
            result = asyncio.run(
                service.execute(
                    manifest=manifest,
                    script=self._entrypoint(manifest),
                    user_message="做间比法，田块10列，CK参数=1,2,8",
                    metadata={"ncols": 8},
                    artifact_context=({"filename": "materials.csv"},),
                    output_context={},
                )
            )

        self.assertEqual(result.status, "completed")
        self.assertEqual(runner.payloads[0]["ncols"], 8)
        self.assertEqual(result.resolution.sources["ncols"].source, "metadata")
        self.assertEqual(result.resolution.sources["ck_spec"].source, "llm_slot_resolver:query")
        prompt_payload = json.loads(prompts[0][prompts[0].find('{"already_resolved"') :])
        target_names = {item["name"] for item in prompt_payload["parameters_to_resolve"]}
        self.assertNotIn("ncols", target_names)

    def test_v2_initial_llm_rejects_unknown_and_artifact_text_claims(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            manifest = self._write_field_design_skill(root)

            async def slot_generator(_prompt: str, **_kwargs) -> str:
                return json.dumps(
                    {
                        "resolved": {
                            "ncols": {"raw_value": "10列", "value": 10, "source": "query"},
                            "ck_spec": {"raw_value": "CK参数=1,2,8", "value": "1,2,8", "source": "query"},
                            "material_data": {"value": {"available": True}, "source": "query"},
                            "unknown": {"value": "bad", "source": "query"},
                        }
                    },
                    ensure_ascii=False,
                )

            service = SkillScriptExecutionService(script_runner=_NoRunRunner(), skill_input_text_generator=slot_generator)
            result = asyncio.run(
                service.execute(
                    manifest=manifest,
                    script=self._entrypoint(manifest),
                    user_message="做间比法，10列，CK参数=1,2,8，材料我口头给你",
                    metadata={},
                    artifact_context=(),
                    output_context={},
                )
            )

        self.assertEqual(result.status, "missing_input")
        self.assertEqual(result.missing, ("material_data",))
        self.assertEqual(result.resolution.payload["ncols"], 10)
        self.assertNotIn("material_data", result.resolution.payload)
        self.assertIn("v2_llm_rejected_unknown_field", result.resolution.diagnostics)
