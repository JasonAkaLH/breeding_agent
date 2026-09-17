from __future__ import annotations

import json
import unittest
from pathlib import Path

from src.core.contracts import CapabilityExecutionRequest
from src.integrations.agent_skills import SkillCatalog, load_input_schemas_for_contract
from src.integrations.agent_skills.missing_input_interrupt import (
    SLOT_COLLECTION_FIELD,
    build_missing_input_interrupt,
    build_missing_input_interrupt_with_question,
    generate_slot_question,
    runtime_missing_input_question_from_payload,
)
from src.integrations.agent_skills.manifest import SkillManifest
from src.integrations.agent_skills.parameters import SkillParameterSpec


def _field_design_manifest():
    catalog = SkillCatalog.from_roots(["skill"])
    manifest = next((manifest for manifest in catalog.skills if manifest.name == "field-design"), None)
    if manifest is None:
        raise unittest.SkipTest("external field-design skill is not present")
    assert manifest.contract is not None
    return manifest


class ProjectSkillMissingInputInterruptContractTest(unittest.TestCase):
    def test_v2_project_skill_missing_input_interrupt_uses_full_schema_snapshot(self) -> None:
        manifest = _field_design_manifest()
        schemas = load_input_schemas_for_contract(manifest.contract)
        self.assertIn("diagonal", schemas)
        self.assertIn("对角线", schemas["diagonal"].inputs["design"].patterns[1])

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
        self.assertIn("对角线", snapshot["inputs"]["design"]["patterns"][1])
        self.assertEqual(snapshot["inputs"]["ncols"]["title"], "田块列数")
        self.assertEqual(snapshot["inputs"]["ncols"]["question"], "请提供田块列数 ncols，例如 ncols=20 或列数20。")
        self.assertEqual(snapshot["inputs"]["ncols"]["validation"], {"min": 1.0})
        self.assertIn("artifact", snapshot["inputs"]["material_data"]["source"]["allowed"])
        self.assertEqual(interrupt.question, "请提供田块列数 ncols，例如 ncols=20 或列数20。")
        self.assertEqual(slot_collection["question_source"], "schema_question")
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
        self.assertNotIn("'parameters':", repr(slot_collection).lower())


class ProjectSkillMissingInputQuestionGeneratorTest(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_missing_input_answer_overrides_v2_schema_question(self) -> None:
        manifest = _field_design_manifest()
        called = False

        async def question_generator(_prompt: str, *, metadata=None) -> str:
            nonlocal called
            called = True
            return json.dumps(
                {
                    "question": "请提供通用 CK 参数。",
                    "ask_fields": ["ck_spec"],
                    "style": "assistant_dialogue",
                },
                ensure_ascii=False,
            )

        interrupt = await build_missing_input_interrupt_with_question(
            request=CapabilityExecutionRequest(
                capability_id="skill.field_design",
                conversation_id="conv-1",
                task_id="task-1",
                node_id="task-1:runtime-question",
            ),
            manifest=manifest,
            skill_name=manifest.name,
            entrypoint="run",
            missing=("ck_spec",),
            resolved_payload={
                "_selected_schema_id": "interval",
                "_selected_entrypoint": "run",
                "design": "interval",
                "ncols": 10,
                "material_data": {"available": True, "count": 1},
            },
            question_text_generator=question_generator,
            runtime_output_payload={
                "ok": False,
                "error": {"type": "missing_input"},
                "missing": ["ck_spec"],
                "answer": "已识别到 2 个 CK。请根据下方 CK 清单补充布局参数。",
                "columns": ["CK编号", "材料编号", "组别"],
                "rows": [
                    {"CK编号": 1, "材料编号": "先玉335", "组别": "东北中熟区"},
                    {"CK编号": 2, "材料编号": "德美亚3号", "组别": "东北中熟区"},
                ],
            },
        )

        self.assertIsNotNone(interrupt)
        self.assertFalse(called)
        self.assertIn("已识别到 2 个 CK", interrupt.question)
        self.assertIn("| CK编号 | 材料编号 | 组别 |", interrupt.question)
        self.assertIn("| 1 | 先玉335 | 东北中熟区 |", interrupt.question)
        slot_collection = interrupt.required_fields[SLOT_COLLECTION_FIELD]
        self.assertEqual(slot_collection["last_question"], interrupt.question)
        self.assertEqual(slot_collection["question_source"], "runtime_missing_input")
        self.assertEqual(slot_collection["missing"], ["ck_spec"])
        self.assertEqual(slot_collection["selected_schema_id"], "interval")

    async def test_runtime_missing_input_without_answer_keeps_schema_question(self) -> None:
        manifest = _field_design_manifest()

        interrupt = await build_missing_input_interrupt_with_question(
            request=CapabilityExecutionRequest(
                capability_id="skill.field_design",
                conversation_id="conv-1",
                task_id="task-1",
                node_id="task-1:runtime-question-empty",
            ),
            manifest=manifest,
            skill_name=manifest.name,
            entrypoint="run",
            missing=("ck_spec",),
            resolved_payload={"_selected_schema_id": "interval", "_selected_entrypoint": "run", "design": "interval", "ncols": 10},
            runtime_output_payload={"ok": False, "error": {"type": "missing_input"}, "missing": ["ck_spec"]},
        )

        self.assertIsNotNone(interrupt)
        self.assertIn("对照位置约束", interrupt.question)
        self.assertIn("不是品种规格", interrupt.question)
        self.assertIn("上传 CSV 或 Excel", interrupt.question)
        self.assertEqual(interrupt.required_fields[SLOT_COLLECTION_FIELD]["question_source"], "schema_question")

    async def test_runtime_missing_input_mismatched_missing_keeps_schema_question(self) -> None:
        manifest = _field_design_manifest()

        interrupt = await build_missing_input_interrupt_with_question(
            request=CapabilityExecutionRequest(
                capability_id="skill.field_design",
                conversation_id="conv-1",
                task_id="task-1",
                node_id="task-1:runtime-question-mismatch",
            ),
            manifest=manifest,
            skill_name=manifest.name,
            entrypoint="run",
            missing=("ck_spec",),
            resolved_payload={"_selected_schema_id": "interval", "_selected_entrypoint": "run", "design": "interval", "ncols": 10},
            runtime_output_payload={
                "ok": False,
                "error": {"type": "missing_input"},
                "missing": ["ncols"],
                "answer": "这个 answer 不应该覆盖 ck_spec 追问。",
            },
        )

        self.assertIsNotNone(interrupt)
        self.assertNotIn("不应该覆盖", interrupt.question)
        self.assertEqual(interrupt.required_fields[SLOT_COLLECTION_FIELD]["question_source"], "schema_question")

    async def test_runtime_missing_input_table_escapes_cells_and_caps_rows(self) -> None:
        rows = [{"name": f"A|B\nC {index}", "api_token": f"secret-{index}", "value": index} for index in range(25)]
        question = runtime_missing_input_question_from_payload(
            {
                "ok": False,
                "error": {"type": "missing_input"},
                "missing": ["choice"],
                "answer": "请选择候选项。",
                "columns": ["name", "api_token", "value"],
                "rows": rows,
            },
            expected_missing=("choice",),
        )

        self.assertIn("A\\|B C 0", question)
        self.assertIn("| name | [已隐藏] | value |", question)
        self.assertIn("| A\\|B C 0 | [已隐藏] | 0 |", question)
        self.assertNotIn("secret-0", question)
        self.assertIn("仅展示前 20 行", question)
        self.assertNotIn("A\\|B C 24", question)

    async def test_v2_explicit_schema_question_is_not_rewritten_by_llm(self) -> None:
        manifest = _field_design_manifest()
        called = False

        async def question_generator(_prompt: str, *, metadata=None) -> str:
            nonlocal called
            called = True
            return json.dumps(
                {
                    "question": "请提供试验的品种规格信息，例如品种名称、数量或处理编号等。",
                    "ask_fields": ["material_data"],
                    "answer_hint": "上传文件即可。",
                    "style": "assistant_dialogue",
                },
                ensure_ascii=False,
            )

        interrupt = await build_missing_input_interrupt_with_question(
            request=CapabilityExecutionRequest(
                capability_id="skill.field_design",
                conversation_id="conv-1",
                task_id="task-1",
                node_id="task-1:question-schema",
                metadata={"deep_thinking": True, "api_key": "SHOULD_NOT_PASS"},
            ),
            manifest=manifest,
            skill_name=manifest.name,
            entrypoint="run",
            missing=("material_data",),
            resolved_payload={"_selected_schema_id": "interval", "_selected_entrypoint": "run", "design": "interval"},
            question_text_generator=question_generator,
        )

        self.assertIsNotNone(interrupt)
        self.assertFalse(called)
        self.assertEqual(interrupt.question, "请上传或提供材料清单文件，推荐列名为 ped_id、hyb_check、set。")
        slot_collection = interrupt.required_fields[SLOT_COLLECTION_FIELD]
        self.assertEqual(slot_collection["question_source"], "schema_question")

    async def test_v2_llm_prompt_includes_slots_when_slots_are_mapping(self) -> None:
        manifest = _field_design_manifest()
        seen_prompts: list[str] = []

        async def question_generator(prompt: str, *, metadata=None) -> str:
            seen_prompts.append(prompt)
            return json.dumps(
                {
                    "question": "请补充列数，例如 12 列。",
                    "ask_fields": ["ncols"],
                    "answer_hint": "直接回答列数即可。",
                    "style": "assistant_dialogue",
                },
                ensure_ascii=False,
            )

        question_payload = await generate_slot_question(
            manifest=manifest,
            slot_collection={
                "schema_version": 2,
                "round": 1,
                "missing": ["ncols"],
                "resolved": {"design": {"value": "interval", "source": "query"}},
                "slots": {
                    "ncols": {
                        "label": "田块列数",
                        "type": "integer",
                        "required_now": True,
                        "status": "missing",
                        "description": "田块列数，例如 10。",
                        "question": "请提供田块列数 ncols，例如 ncols=10 或列数10。",
                    }
                },
            },
            text_generator=question_generator,
            metadata={"model_edition": "expert"},
        )

        self.assertEqual(question_payload["question"], "请补充列数，例如 12 列。")
        self.assertIn("\"label\": \"田块列数\"", seen_prompts[0])
        self.assertIn("\"question\": \"请提供田块列数 ncols，例如 ncols=10 或列数10。\"", seen_prompts[0])

    async def test_legacy_llm_generated_slot_question_receives_only_frontend_llm_options_metadata(self) -> None:
        manifest = SkillManifest(
            name="legacy-skill",
            description="Legacy skill",
            triggers=(),
            body="",
            source_path=Path("legacy/SKILL.md"),
            parameters={"query": SkillParameterSpec(name="query", required=True)},
        )
        seen_metadata: list[dict] = []

        async def question_generator(_prompt: str, *, metadata=None) -> str:
            seen_metadata.append(dict(metadata or {}))
            return json.dumps(
                {
                    "question": "请补充问题。",
                    "ask_fields": ["query"],
                    "answer_hint": "直接回答即可。",
                    "style": "assistant_dialogue",
                },
                ensure_ascii=False,
            )

        interrupt = await build_missing_input_interrupt_with_question(
            request=CapabilityExecutionRequest(
                capability_id="skill.legacy",
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
            missing=("query",),
            question_text_generator=question_generator,
        )

        self.assertIsNotNone(interrupt)
        self.assertEqual(interrupt.question, "请补充问题。")
        self.assertEqual(
            seen_metadata,
            [{"deep_thinking": True, "main_agent_reasoning_effort": "max", "model_edition": "expert"}],
        )


if __name__ == "__main__":
    unittest.main()
