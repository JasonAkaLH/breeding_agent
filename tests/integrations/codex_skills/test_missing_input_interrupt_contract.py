from __future__ import annotations

import unittest

from src.core.contracts import CapabilityExecutionRequest
from src.integrations.codex_skills import SkillCatalog
from src.integrations.codex_skills.missing_input_interrupt import build_missing_input_interrupt


class ProjectSkillMissingInputInterruptContractTest(unittest.TestCase):
    def test_all_project_skills_have_specific_missing_input_interrupt_metadata(self) -> None:
        catalog = SkillCatalog.from_roots(["skill"])
        manifests = {manifest.name: manifest for manifest in catalog.skills}
        expected_missing = {
            "field-analysis": ("field_data", "design"),
            "field-design": ("material_data", "design"),
            "ocr": ("file_path",),
            "rice-genie": ("rice_input",),
            "sql-query": ("query",),
        }
        self.assertEqual(set(expected_missing).difference(manifests), set())

        for skill_name, missing_fields in expected_missing.items():
            manifest = manifests[skill_name]
            interrupt = build_missing_input_interrupt(
                request=CapabilityExecutionRequest(
                    capability_id=f"skill.{skill_name.replace('-', '_')}",
                    conversation_id="conv-1",
                    task_id="task-1",
                    node_id=f"task-1:{skill_name}",
                    input_payload={"message_id": "msg-1"},
                ),
                manifest=manifest,
                skill_name=skill_name,
                entrypoint="contract-test",
                missing=missing_fields,
            )
            self.assertIsNotNone(interrupt, skill_name)
            self.assertNotIn("正在等待任务给出补充信息", interrupt.question)
            self.assertTrue(interrupt.question.strip(), skill_name)
            self.assertEqual(set(interrupt.required_fields), set(missing_fields))
            for field in missing_fields:
                self.assertIn("type", interrupt.required_fields[field], f"{skill_name}:{field}")
                self.assertIn("description", interrupt.required_fields[field], f"{skill_name}:{field}")

        upload_fields = {
            ("field-analysis", "field_data"),
            ("field-design", "material_data"),
            ("ocr", "file_path"),
            ("rice-genie", "rice_input"),
        }
        for skill_name, field in upload_fields:
            manifest = manifests[skill_name]
            interrupt = build_missing_input_interrupt(
                request=CapabilityExecutionRequest(
                    capability_id=f"skill.{skill_name.replace('-', '_')}",
                    conversation_id="conv-1",
                    task_id="task-1",
                    node_id=f"task-1:{skill_name}:upload",
                ),
                manifest=manifest,
                skill_name=skill_name,
                entrypoint="contract-test",
                missing=(field,),
            )
            self.assertIs(interrupt.required_fields[field].get("accepts_upload"), True, f"{skill_name}:{field}")


if __name__ == "__main__":
    unittest.main()
