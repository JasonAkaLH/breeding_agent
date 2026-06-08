from __future__ import annotations

import json
import unittest

from src.core.contracts import CapabilityExecutionRequest
from src.integrations.agent_skills import SkillCatalog, load_input_schemas_for_contract
from src.integrations.agent_skills.missing_input_interrupt import (
    SLOT_COLLECTION_FIELD,
    build_missing_input_interrupt,
    build_missing_input_interrupt_with_question,
)


def _field_design_manifest():
    catalog = SkillCatalog.from_roots(["skill"])
    manifest = next(manifest for manifest in catalog.skills if manifest.name == "field-design")
    assert manifest.contract is not None
    return manifest


class ProjectSkillMissingInputInterruptContractTest(unittest.TestCase):
    def test_v2_project_skill_missing_input_interrupt_uses_full_schema_snapshot(self) -> None:
        manifest = _field_design_manifest()
        schemas = load_input_schemas_for_contract(manifest.contract)
        self.assertIn("diagonal", schemas)
        self.assertIn("对角线增广", schemas["diagonal"].inputs["design"].aliases)

        interrupt = build_missing_input_interrupt(
            request=CapabilityExecutionRequest(
                capability_id="skill.field_design",
                conversation_id="conv-1",
                task_id="task-1",
                node_id="task-1:field-design",
                input_payload={"message_id": "msg-1"},
            ),
            manifest=manifest,
            skill_name=manifest.name,
            entrypoint="run",
            missing=("ncols",),
            resolved_payload={
                "_selected_schema_id": "diagonal",
                "_selected_entrypoint": "run",
                "design": "diagonal",
                "material_data": {
                    "available": True,
                    "count": 1,
                    "content": "RAW_ARTIFACT_CONTENT_SHOULD_NOT_LEAK",
                    "content_base64": "RAW_BASE64_SHOULD_NOT_LEAK",
                    "token": "SECRET_TOKEN_SHOULD_NOT_LEAK",
                },
            },
        )

        self.assertIsNotNone(interrupt)
        self.assertEqual(set(interrupt.required_fields), {SLOT_COLLECTION_FIELD})
        slot_collection = interrupt.required_fields[SLOT_COLLECTION_FIELD]
        self.assertEqual(slot_collection["schema_version"], 2)
        self.assertEqual(slot_collection["kind"], "input_collection")
        self.assertEqual(slot_collection["selected_schema_id"], "diagonal")
        self.assertEqual(slot_collection["selected_entrypoint"], "run")
        self.assertEqual(slot_collection["missing"], ["ncols"])
        snapshot = slot_collection["schema_snapshot"]
        self.assertEqual(snapshot["schema_id"], "diagonal")
        self.assertEqual(snapshot["inputs"]["design"]["const"], "diagonal")
        self.assertIn("对角线增广", snapshot["inputs"]["design"]["aliases"])
        self.assertEqual(snapshot["inputs"]["ncols"]["validation"], {"min": 1, "max": 1000})
        self.assertEqual(snapshot["inputs"]["material_data"]["source"]["allowed"], ["artifact"])
        rendered = repr(slot_collection)
        self.assertNotIn("RAW_ARTIFACT_CONTENT_SHOULD_NOT_LEAK", rendered)
        self.assertNotIn("RAW_BASE64_SHOULD_NOT_LEAK", rendered)
        self.assertNotIn("SECRET_TOKEN_SHOULD_NOT_LEAK", rendered)

    def test_v2_schema_selection_interrupt_lists_allowed_schemas_without_legacy_parameters(self) -> None:
        manifest = _field_design_manifest()
        interrupt = build_missing_input_interrupt(
            request=CapabilityExecutionRequest(
                capability_id="skill.field_design",
                conversation_id="conv-1",
                task_id="task-1",
                node_id="task-1:field-design:schema-selection",
                input_payload={"message_id": "msg-1"},
            ),
            manifest=manifest,
            skill_name=manifest.name,
            entrypoint="run",
            missing=("design",),
        )

        self.assertEqual(set(interrupt.required_fields), {SLOT_COLLECTION_FIELD})
        slot_collection = interrupt.required_fields[SLOT_COLLECTION_FIELD]
        self.assertEqual(slot_collection["schema_version"], 2)
        self.assertEqual(slot_collection["kind"], "schema_selection")
        allowed = slot_collection["schema_snapshot"]["allowed_schemas"]
        by_id = {item["schema_id"]: item for item in allowed}
        self.assertIn("diagonal", by_id)
        self.assertIn("对角线增广", by_id["diagonal"]["aliases"])
        self.assertEqual(slot_collection["missing"], ["design"])
        self.assertNotIn("parameters", repr(slot_collection).lower())


class ProjectSkillMissingInputQuestionGeneratorTest(unittest.IsolatedAsyncioTestCase):
    async def test_v2_llm_generated_slot_question_receives_only_frontend_llm_options_metadata(self) -> None:
        manifest = _field_design_manifest()
        seen_metadata: list[dict] = []

        async def question_generator(_prompt: str, *, metadata=None) -> str:
            seen_metadata.append(dict(metadata or {}))
            return json.dumps(
                {
                    "question": "请补充列数，例如 12 列。",
                    "ask_fields": ["ncols"],
                    "answer_hint": "直接回答列数即可。",
                    "style": "assistant_dialogue",
                },
                ensure_ascii=False,
            )

        interrupt = await build_missing_input_interrupt_with_question(
            request=CapabilityExecutionRequest(
                capability_id="skill.field_design",
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
            entrypoint="run",
            missing=("ncols",),
            resolved_payload={"_selected_schema_id": "diagonal", "_selected_entrypoint": "run", "design": "diagonal"},
            question_text_generator=question_generator,
        )

        self.assertIsNotNone(interrupt)
        self.assertEqual(interrupt.question, "请补充列数，例如 12 列。")
        slot_collection = interrupt.required_fields[SLOT_COLLECTION_FIELD]
        self.assertEqual(slot_collection["schema_version"], 2)
        self.assertEqual(slot_collection["ask_fields"], ["ncols"])
        self.assertEqual(
            seen_metadata,
            [{"deep_thinking": True, "main_agent_reasoning_effort": "max", "model_edition": "expert"}],
        )


if __name__ == "__main__":
    unittest.main()
