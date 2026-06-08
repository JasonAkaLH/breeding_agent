from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from src.core.models import SlotCollection
from src.integrations.agent_skills.contract import SkillResourceRef
from src.integrations.agent_skills.input_schema import parse_input_schema_file
from src.integrations.agent_skills.slot_state import (
    SlotStateTransitionError,
    apply_extraction_result_to_collection,
    build_backend_slot_extraction,
    build_history_recall_prompt,
    build_normal_extraction_prompt,
    build_schema_snapshot,
    initialize_input_collection,
    merge_slot_extraction_results,
    parse_slot_extraction_response,
    redact_prompt_safe,
    should_trigger_history_recall,
    transition_slot_collection,
)


NOW = datetime(2026, 6, 8, 11, 0, 0)


def _schema(text: str):
    tmp = tempfile.TemporaryDirectory()
    path = Path(tmp.name) / "schema.input.yaml"
    path.write_text(text, encoding="utf-8")
    schema = parse_input_schema_file(path)
    return tmp, schema


def _field_design_schema():
    return _schema(
        """
schema_id: diagonal
title: 对角线增广设计
description: 生成对角线增广田间设计。
inputs:
  design:
    type: string
    required: true
    const: diagonal
    aliases: [diagonal, 对角线, 对角线增广]
    description: 设计类型。
    clarification: {hint: 请选择设计类型, examples: [对角线增广]}
  ncols:
    type: integer
    required: true
    aliases: [ncols, 列数, 田块列数]
    patterns: ['(\\d+)\\s*列']
    validation: {min: 1, max: 1000, message: 列数必须大于 0}
  ck_ratio:
    type: string
    required: false
    default: A
    enum: [A, B]
  material_data:
    type: artifact
    required: true
    source: {allowed: [artifact]}
    aliases: [材料清单, material_data]
    reference_resource: material_data
  ck_spec:
    type: string
    required_when: {design: interval}
    aliases: [CK参数]
  provider_config:
    type: string
    expose: false
constraints:
  - dependencies: {ck_spec: [ncols]}
slot_policy: {max_rounds: 4}
entrypoint_mapping: run
""",
    )


def _collection(schema=None) -> SlotCollection:
    if schema is None:
        tmp, schema = _field_design_schema()
        tmp.cleanup()
    return SlotCollection(
        collection_id="slot-collection-test",
        task_id="task-test",
        node_id="node-test",
        conversation_id="conv-test",
        capability_id="skill.field_design",
        skill_name="field-design",
        kind="input_collection",
        status="waiting_for_user",
        round=1,
        revision=0,
        selected_schema_id=schema.schema_id,
        selected_entrypoint="run",
        schema_digest="sha256:test",
        schema_snapshot=build_schema_snapshot(schema),
        slots={
            "design": {"status": "missing"},
            "ncols": {"status": "missing"},
            "material_data": {"status": "resolved", "source": "artifact"},
        },
        resolved={"material_data": {"raw_value": {"upload_id": "upl-1"}, "value": {"upload_id": "upl-1"}, "source": "artifact"}},
        missing=("design", "ncols"),
        invalid=(),
        last_question="请补充设计类型和田块列数。",
        created_at=NOW,
        updated_at=NOW,
    )


class SlotStateMachineTest(unittest.TestCase):
    def test_schema_snapshot_includes_complete_exposed_context(self) -> None:
        tmp, schema = _field_design_schema()
        self.addCleanup(tmp.cleanup)
        snapshot = build_schema_snapshot(
            schema,
            resources={
                "material_data": SkillResourceRef(
                    resource_id="material_data",
                    path="references/material-data.md",
                    title="材料表字段",
                    audience=("main_agent", "slot_question"),
                )
            },
        )

        self.assertEqual(snapshot["schema_id"], "diagonal")
        self.assertNotIn("provider_config", snapshot["inputs"])
        design = snapshot["inputs"]["design"]
        self.assertEqual(design["const"], "diagonal")
        self.assertIn("对角线增广", design["aliases"])
        self.assertEqual(design["clarification"]["examples"], ["对角线增广"])
        ncols = snapshot["inputs"]["ncols"]
        self.assertTrue(ncols["required"])
        self.assertEqual(ncols["validation"]["min"], 1.0)
        self.assertEqual(snapshot["inputs"]["ck_ratio"]["enum"], ["A", "B"])
        self.assertEqual(snapshot["inputs"]["ck_spec"]["required_when"], {"design": "interval"})
        self.assertEqual(snapshot["inputs"]["material_data"]["source"]["allowed"], ["artifact"])
        self.assertEqual(snapshot["constraints"], [{"dependencies": {"ck_spec": ["ncols"]}}])
        self.assertEqual(snapshot["resources"]["material_data"]["title"], "材料表字段")

    def test_collection_initialization_and_transition_guards_emit_events(self) -> None:
        tmp, schema = _field_design_schema()
        self.addCleanup(tmp.cleanup)
        collection, event = initialize_input_collection(
            collection_id="slot-init",
            task_id="task-init",
            node_id="node-init",
            conversation_id="conv-init",
            capability_id="skill.field_design",
            skill_name="field-design",
            schema=schema,
            selected_entrypoint="run",
            now=NOW,
        )
        self.assertEqual(collection.status, "collecting")
        self.assertEqual(collection.revision, 0)
        self.assertEqual(event.event_type, "slot.collection_started")
        self.assertEqual(event.revision, 0)

        waiting, prompt_event = transition_slot_collection(
            collection,
            to_status="waiting_for_user",
            event_type="slot.prompt_generated",
            payload={"question": "请补充田块列数。"},
            now=NOW,
        )
        self.assertEqual(waiting.revision, 1)
        self.assertEqual(prompt_event.revision, 1)
        self.assertEqual(prompt_event.payload["question"], "请补充田块列数。")

        completed, _ = transition_slot_collection(
            waiting,
            to_status="cancelled",
            event_type="slot.collection_cancelled",
            now=NOW,
        )
        with self.assertRaises(SlotStateTransitionError):
            transition_slot_collection(
                completed,
                to_status="waiting_for_user",
                event_type="slot.prompt_generated",
                now=NOW,
            )

    def test_normal_extraction_prompt_is_schema_complete_and_redacted(self) -> None:
        collection = _collection()
        prompt = build_normal_extraction_prompt(
            collection,
            current_user_answer="对角线增广 api_key=sk-secret /Users/yinpeihai/private.csv",
            artifact_summaries=(
                {
                    "upload_id": "upl-1",
                    "filename": "/Users/yinpeihai/material.csv",
                    "content": "ped_id,hyb_check,set\nA001,0,A\n",
                    "content_base64": "cGVkX2lk",
                },
            ),
        )
        payload = json.loads(prompt)

        self.assertEqual(payload["mode"], "normal_extraction")
        self.assertEqual(payload["slot_collection"]["schema_snapshot"]["inputs"]["design"]["const"], "diagonal")
        self.assertIn("对角线增广", payload["current_user_answer"])
        encoded = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("sk-secret", encoded)
        self.assertNotIn("/Users/yinpeihai", encoded)
        self.assertNotIn("ped_id,hyb_check,set", encoded)
        self.assertIn("[REDACTED_SECRET]", encoded)
        self.assertIn("[REDACTED_PATH]", encoded)

    def test_extraction_candidate_parsing_canonicalizes_raw_design_and_integer(self) -> None:
        tmp, schema = _field_design_schema()
        self.addCleanup(tmp.cleanup)
        collection = _collection(schema)
        raw_response = json.dumps(
            {
                "resolved": {
                    "design": {"raw": "对角线增广", "value": "对角线增广", "source": "current_answer"},
                    "ncols": {"raw_value": "12列", "value": "12列"},
                    "unknown": {"raw": "x", "value": "x"},
                },
                "missing": [],
                "invalid": [],
            },
            ensure_ascii=False,
        )

        extraction = parse_slot_extraction_response(raw_response, collection)
        self.assertIn("unknown_field:unknown", extraction.diagnostics)
        next_collection, event = apply_extraction_result_to_collection(replace(collection, status="validating"), schema, extraction, now=NOW)

        self.assertEqual(next_collection.status, "ready")
        self.assertEqual(next_collection.missing, ())
        self.assertEqual(next_collection.invalid, ())
        self.assertEqual(next_collection.resolved["design"]["raw_value"], "对角线增广")
        self.assertEqual(next_collection.resolved["design"]["value"], "diagonal")
        self.assertEqual(next_collection.resolved["ncols"]["raw_value"], "12列")
        self.assertEqual(next_collection.resolved["ncols"]["value"], 12)
        self.assertEqual(event.event_type, "slot.collection_ready")

    def test_backend_merge_resolves_scalar_and_current_upload_without_overfilling_strings(self) -> None:
        tmp, schema = _field_design_schema()
        self.addCleanup(tmp.cleanup)
        collection = replace(
            _collection(schema),
            resolved={"design": {"raw_value": "对角线增广", "value": "diagonal", "source": "schema_selection"}},
            missing=("ncols", "material_data", "ck_spec"),
            slots={
                "ncols": {"name": "ncols", "type": "integer", "status": "missing"},
                "material_data": {"name": "material_data", "type": "artifact", "status": "missing"},
                "ck_spec": {"name": "ck_spec", "type": "string", "status": "missing"},
            },
        )
        llm_extraction = parse_slot_extraction_response(
            json.dumps({"resolved": {"ncols": {"raw_value": "田块12列", "value": "12列", "source": "current_answer"}}}, ensure_ascii=False),
            collection,
        )

        backend = build_backend_slot_extraction(
            collection,
            schema,
            current_user_answer="田块12列",
            current_upload_ids=("upl-current",),
            artifact_summaries=(
                {
                    "upload_id": "upl-current",
                    "filename": "interval_realistic_two_sets.csv",
                    "preview": {"columns": ["ped_id", "hyb_check", "set"], "row_count": 60},
                },
            ),
        )
        merged = merge_slot_extraction_results(llm_extraction, backend, collection=collection)
        next_collection, _ = apply_extraction_result_to_collection(replace(collection, status="validating"), schema, merged, now=NOW)

        self.assertEqual(next_collection.resolved["ncols"]["value"], 12)
        self.assertEqual(next_collection.resolved["material_data"]["source"], "task_attachment")
        self.assertEqual(next_collection.resolved["material_data"]["value"]["upload_ids"], ["upl-current"])
        self.assertNotIn("ck_spec", next_collection.resolved)

    def test_history_recall_resolves_prior_upload_from_backend_ledger_not_llm_artifact_claim(self) -> None:
        tmp, schema = _field_design_schema()
        self.addCleanup(tmp.cleanup)
        collection = replace(
            _collection(schema),
            resolved={"design": {"raw_value": "对角线增广", "value": "diagonal", "source": "schema_selection"}, "ncols": {"raw_value": "12列", "value": 12, "source": "history"}},
            missing=("material_data",),
            slots={"material_data": {"name": "material_data", "type": "artifact", "status": "missing"}},
        )
        llm_extraction = parse_slot_extraction_response(
            json.dumps({"resolved": {"material_data": {"raw_value": "之前上传过", "value": {"upload_ids": ["upl-old"]}, "source": "history"}}}, ensure_ascii=False),
            collection,
        )
        self.assertEqual(llm_extraction.resolved["material_data"].source, "llm_artifact_claim")

        backend = build_backend_slot_extraction(
            collection,
            schema,
            current_user_answer="我之前不是上传过了吗",
            artifact_summaries=(
                {"upload_id": "upl-old", "filename": "materials.csv", "preview": {"columns": ["ped_id"], "row_count": 1}},
            ),
            accepted_answer_summaries=({"text": "材料在这个文件", "upload_ids": ["upl-old"]},),
            history_recall=True,
        )
        merged = merge_slot_extraction_results(llm_extraction, backend, collection=collection)
        next_collection, _ = apply_extraction_result_to_collection(replace(collection, status="validating"), schema, merged, now=NOW)

        self.assertEqual(next_collection.status, "ready")
        self.assertEqual(next_collection.resolved["material_data"]["source"], "task_attachment")
        self.assertEqual(next_collection.resolved["material_data"]["value"]["upload_ids"], ["upl-old"])

    def test_validation_failure_loop_marks_invalid_and_keeps_raw_value(self) -> None:
        tmp, schema = _field_design_schema()
        self.addCleanup(tmp.cleanup)
        collection = _collection(schema)
        extraction = parse_slot_extraction_response(
            json.dumps(
                {
                    "resolved": {
                        "design": {"raw": "对角线增广", "value": "diagonal"},
                        "ncols": {"raw": "0列", "value": 0},
                    }
                },
                ensure_ascii=False,
            ),
            collection,
        )

        next_collection, event = apply_extraction_result_to_collection(replace(collection, status="validating"), schema, extraction, now=NOW)

        self.assertEqual(next_collection.status, "waiting_for_user")
        self.assertIn("ncols", next_collection.missing)
        self.assertEqual(next_collection.resolved["ncols"]["raw_value"], "0列")
        self.assertEqual(next_collection.slots["ncols"]["status"], "invalid")
        self.assertEqual(next_collection.invalid[0]["field"], "ncols")
        self.assertEqual(next_collection.invalid[0]["reason"], "min")
        self.assertEqual(event.event_type, "slot.validation_failed")

    def test_llm_artifact_claim_is_rejected_even_when_source_says_artifact(self) -> None:
        tmp, schema = _field_design_schema()
        self.addCleanup(tmp.cleanup)
        collection = replace(
            _collection(schema),
            status="validating",
            resolved={},
            missing=("material_data",),
            slots={"material_data": {"name": "material_data", "type": "artifact", "status": "missing"}},
        )
        extraction = parse_slot_extraction_response(
            json.dumps(
                {
                    "resolved": {
                        "material_data": {
                            "raw_value": {"upload_ids": ["forged-upload"]},
                            "value": {"upload_ids": ["forged-upload"]},
                            "source": "artifact",
                        }
                    }
                },
                ensure_ascii=False,
            ),
            collection,
        )

        next_collection, event = apply_extraction_result_to_collection(collection, schema, extraction, now=NOW)

        self.assertEqual(next_collection.status, "waiting_for_user")
        self.assertIn("material_data", next_collection.missing)
        self.assertEqual(next_collection.invalid[0]["reason"], "artifact_source_denied")
        self.assertEqual(event.event_type, "slot.validation_failed")

    def test_history_recall_prompt_is_separate_and_rule_triggered(self) -> None:
        collection = _collection()
        self.assertTrue(should_trigger_history_recall("我之前不是告诉过你了吗？"))
        self.assertFalse(should_trigger_history_recall("对角线增广，12列"))

        prompt = build_history_recall_prompt(
            collection,
            current_user_answer="我之前不是告诉过你了吗？",
            accepted_answer_summaries=(
                {"message_id": "msg-1", "text": "材料已经上传，设计是对角线增广"},
                {"message_id": "msg-2", "text": "token=secret"},
            ),
        )
        payload = json.loads(prompt)

        self.assertEqual(payload["mode"], "history_recall_extraction")
        self.assertEqual(payload["current_user_answer"], "我之前不是告诉过你了吗？")
        self.assertIn("accepted_answer_summaries", payload)
        self.assertNotIn("token=secret", json.dumps(payload, ensure_ascii=False))

    def test_redact_prompt_safe_removes_sensitive_values_and_raw_artifact_content(self) -> None:
        redacted = redact_prompt_safe(
            {
                "provider_config": {"api_key": "sk-secret"},
                "database_url": "postgresql://user:pass@localhost/db",
                "cookie": "session=secret",
                "path": "/Users/yinpeihai/material.csv",
                "artifact": {"content": "raw,csv\n1,2", "content_base64": "cmF3"},
                "safe": "对角线增广",
            }
        )
        encoded = json.dumps(redacted, ensure_ascii=False)

        self.assertIn("对角线增广", encoded)
        self.assertNotIn("sk-secret", encoded)
        self.assertNotIn("postgresql://user", encoded)
        self.assertNotIn("session=secret", encoded)
        self.assertNotIn("/Users/yinpeihai", encoded)
        self.assertNotIn("raw,csv", encoded)
