from __future__ import annotations

from src.integrations.agent_skills.missing_input_interrupt import SLOT_COLLECTION_FIELD
from tests.api.support import APITestCase


class SkillSlotCollectionV2APITest(APITestCase):
    async def test_ambiguous_schema_opens_slot_collection_v2(self) -> None:
        root = self.workspace / "skill"
        skill = root / "design"
        (skill / "scripts").mkdir(parents=True)
        (skill / "schemas").mkdir()
        (skill / "SKILL.md").write_text("---\nname: design\ndescription: design\n---\n\n# Design\n", encoding="utf-8")
        (skill / "skill.contract.yaml").write_text("""
contract_version: '2'
capability: {id: skill.design, display_name: Design}
runtime: {mode: python_subprocess, answer_mode: direct}
schema_selector: {strategy: deterministic_then_llm, selector_field: design}
entrypoints: {run: {path: scripts/run.py}}
input_schemas:
  rcbd: {path: schemas/rcbd.input.yaml, aliases: [rcbd], entrypoint: run}
  interval: {path: schemas/interval.input.yaml, aliases: [interval], entrypoint: run}
""", encoding="utf-8")
        (skill / "schemas" / "rcbd.input.yaml").write_text("schema_id: rcbd\ninputs: {design: {type: string, required: true, const: rcbd}}\n", encoding="utf-8")
        (skill / "schemas" / "interval.input.yaml").write_text("schema_id: interval\ninputs: {design: {type: string, required: true, const: interval}, ck_spec: {type: string, required: true}}\n", encoding="utf-8")
        (skill / "scripts" / "run.py").write_text("import json\nprint(json.dumps({'answer':'ok'}))\n", encoding="utf-8")
        await self.reconfigure_runtime(skill_roots=(root,), public_skill_roots=(root,), enable_skill_input_llm=False)
        response = await self.submit_message(content="做田间试验设计", capability_id="skill.design")
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]
        await self.wait_for_condition(lambda: self.runtime.list_interrupts(task_id))
        interrupts = await self.runtime.list_interrupts(task_id)
        collection = interrupts[0]["required_fields"][SLOT_COLLECTION_FIELD]
        self.assertEqual(collection["schema_version"], 2)
        self.assertEqual(collection["selected_entrypoint"], "run")
        self.assertEqual(collection["missing"], ["design"])

