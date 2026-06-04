from __future__ import annotations

import json
import unittest

from src.core.contracts import CapabilityExecutionRequest
from src.integrations.agent_skills import SkillCatalog
from src.integrations.agent_skills.missing_input_interrupt import (
    SLOT_COLLECTION_FIELD,
    build_missing_input_interrupt,
    build_missing_input_interrupt_with_question,
)


class ProjectSkillMissingInputInterruptContractTest(unittest.TestCase):
    def test_all_project_skills_have_specific_missing_input_interrupt_metadata(self) -> None:
        catalog = SkillCatalog.from_roots(["skill"])
        manifests = {manifest.name: manifest for manifest in catalog.skills}
        expected_missing = {
            name: tuple(field_name for field_name, spec in manifest.parameters.items() if spec.required)
            for name, manifest in manifests.items()
        }
        expected_missing = {name: fields for name, fields in expected_missing.items() if fields}
        self.assertTrue(expected_missing)

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
            self.assertEqual(set(interrupt.required_fields), {*missing_fields, SLOT_COLLECTION_FIELD})
            slot_collection = interrupt.required_fields[SLOT_COLLECTION_FIELD]
            self.assertEqual(slot_collection["schema_version"], 1, skill_name)
            self.assertEqual(slot_collection["round"], 1, skill_name)
            self.assertEqual(set(slot_collection["missing"]), set(missing_fields), skill_name)
            self.assertEqual(slot_collection["question_source"], "fallback", skill_name)
            self.assertNotIn("请在输入框补充后继续当前任务", interrupt.question)
            for field in missing_fields:
                self.assertIn("type", interrupt.required_fields[field], f"{skill_name}:{field}")
                self.assertIn("description", interrupt.required_fields[field], f"{skill_name}:{field}")
                slot = next(item for item in slot_collection["slots"] if item["name"] == field)
                self.assertEqual(slot["status"], "missing", f"{skill_name}:{field}")

        upload_fields = {
            (name, field_name)
            for name, manifest in manifests.items()
            for field_name, spec in manifest.parameters.items()
            if spec.required and spec.type == "artifact"
        }
        self.assertTrue(upload_fields)
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
            slot_collection = interrupt.required_fields[SLOT_COLLECTION_FIELD]
            self.assertEqual(slot_collection["missing"], [field])
            self.assertTrue(any(slot["name"] == field and slot["status"] == "missing" for slot in slot_collection["slots"]))

    def test_slot_collection_preserves_resolved_safe_values_and_omits_sensitive_content(self) -> None:
        catalog = SkillCatalog.from_roots(["skill"])
        manifest = next(manifest for manifest in catalog.skills if manifest.parameters)
        missing_fields = tuple(field for field, spec in manifest.parameters.items() if spec.required)
        if not missing_fields:
            self.skipTest("project Skill parameters have no required fields")

        resolved_payload = {
            "already_safe": "value",
            "material_data": {
                "available": True,
                "count": 1,
                "content": "RAW_ARTIFACT_CONTENT_SHOULD_NOT_LEAK",
                "content_base64": "RAW_BASE64_SHOULD_NOT_LEAK",
                "token": "SECRET_TOKEN_SHOULD_NOT_LEAK",
            },
        }
        interrupt = build_missing_input_interrupt(
            request=CapabilityExecutionRequest(
                capability_id=f"skill.{manifest.name.replace('-', '_')}",
                conversation_id="conv-1",
                task_id="task-1",
                node_id="task-1:safe-slot",
                input_payload={"message_id": "msg-1"},
            ),
            manifest=manifest,
            skill_name=manifest.name,
            entrypoint="contract-test",
            missing=missing_fields,
            resolved_payload=resolved_payload,
        )

        rendered = repr(interrupt.required_fields[SLOT_COLLECTION_FIELD])
        self.assertNotIn("RAW_ARTIFACT_CONTENT_SHOULD_NOT_LEAK", rendered)
        self.assertNotIn("RAW_BASE64_SHOULD_NOT_LEAK", rendered)
        self.assertNotIn("SECRET_TOKEN_SHOULD_NOT_LEAK", rendered)


class ProjectSkillMissingInputQuestionGeneratorTest(unittest.IsolatedAsyncioTestCase):
    async def test_llm_generated_slot_question_receives_only_frontend_llm_options_metadata(self) -> None:
        catalog = SkillCatalog.from_roots(["skill"])
        manifest = next(manifest for manifest in catalog.skills if any(spec.required for spec in manifest.parameters.values()))
        missing_field = next(field for field, spec in manifest.parameters.items() if spec.required)
        seen_metadata: list[dict] = []

        async def question_generator(_prompt: str, *, metadata=None) -> str:
            seen_metadata.append(dict(metadata or {}))
            return json.dumps(
                {
                    "question": "请补充缺失参数。",
                    "ask_fields": [missing_field],
                    "answer_hint": "直接回答参数值即可。",
                    "style": "assistant_dialogue",
                },
                ensure_ascii=False,
            )

        interrupt = await build_missing_input_interrupt_with_question(
            request=CapabilityExecutionRequest(
                capability_id=f"skill.{manifest.name.replace('-', '_')}",
                conversation_id="conv-1",
                task_id="task-1",
                node_id="task-1:question-metadata",
                metadata={
                    "deep_thinking": True,
                    "main_agent_reasoning_effort": "max",
                    "model_edition": "expert",
                    "api_key": "SHOULD_NOT_PASS",
                    "upload_ids": ["upl-1"],
                },
            ),
            manifest=manifest,
            skill_name=manifest.name,
            entrypoint="contract-test",
            missing=(missing_field,),
            question_text_generator=question_generator,
        )

        self.assertIsNotNone(interrupt)
        self.assertEqual(interrupt.question, "请补充缺失参数。")
        self.assertEqual(
            seen_metadata,
            [{"deep_thinking": True, "main_agent_reasoning_effort": "max", "model_edition": "expert"}],
        )


if __name__ == "__main__":
    unittest.main()
