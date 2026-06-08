from __future__ import annotations

import json
from dataclasses import replace

from src.core.enums import InterruptStatus
from src.integrations.agent_skills.missing_input_interrupt import SLOT_COLLECTION_FIELD, SLOT_COLLECTION_REF_FIELD
from tests.api.support import APITestCase


class SkillSlotCollectionV2APITest(APITestCase):
    def _write_diagonal_skill(self, root):
        skill = root / "field-design"
        (skill / "scripts").mkdir(parents=True)
        (skill / "schemas").mkdir()
        (skill / "SKILL.md").write_text("---\nname: field-design\ndescription: design\n---\n\n# Design\n", encoding="utf-8")
        (skill / "skill.contract.yaml").write_text("""
contract_version: '2'
capability: {id: skill.field_design, display_name: Field Design}
runtime: {mode: python_subprocess, answer_mode: direct}
schema_selector: {strategy: deterministic_then_llm, selector_field: design}
entrypoints: {run: {path: scripts/run.py}}
input_schemas:
  diagonal: {path: schemas/diagonal.input.yaml, aliases: [diagonal, 对角线, 对角线增广], entrypoint: run}
""", encoding="utf-8")
        (skill / "schemas" / "diagonal.input.yaml").write_text(
            """
schema_id: diagonal
inputs:
  design: {type: string, required: true, const: diagonal, aliases: [diagonal, 对角线, 对角线增广]}
  ncols: {type: integer, required: true, aliases: [ncols, 列数], validation: {min: 1, max: 1000}}
""",
            encoding="utf-8",
        )
        (skill / "scripts" / "run.py").write_text(
            "import json, sys\npayload=json.load(sys.stdin)\nprint(json.dumps({'answer':'ok','design':payload.get('design'),'ncols':payload.get('ncols')}, ensure_ascii=False))\n",
            encoding="utf-8",
        )
        return skill

    def _write_field_design_upload_skill(self, root):
        skill = root / "field-design"
        (skill / "scripts").mkdir(parents=True)
        (skill / "schemas").mkdir()
        (skill / "SKILL.md").write_text("---\nname: field-design\ndescription: design\n---\n\n# Design\n", encoding="utf-8")
        (skill / "skill.contract.yaml").write_text("""
contract_version: '2'
capability: {id: skill.field_design, display_name: Field Design}
runtime: {mode: python_subprocess, answer_mode: direct}
schema_selector: {strategy: deterministic_then_llm, selector_field: design}
entrypoints: {run: {path: scripts/run.py}}
input_schemas:
  diagonal: {path: schemas/diagonal.input.yaml, aliases: [diagonal, 对角线, 对角线增广], entrypoint: run}
  interval: {path: schemas/interval.input.yaml, aliases: [interval, 间比法], entrypoint: run}
""", encoding="utf-8")
        (skill / "schemas" / "diagonal.input.yaml").write_text(
            """
schema_id: diagonal
inputs:
  design: {type: string, required: true, const: diagonal, aliases: [diagonal, 对角线, 对角线增广]}
  ncols: {type: integer, required: true, aliases: [ncols, 列数, 田块列数], validation: {min: 1, max: 1000}}
  material_data: {type: artifact, required: true, source: {allowed: [artifact]}, aliases: [材料清单, material_data]}
""",
            encoding="utf-8",
        )
        (skill / "schemas" / "interval.input.yaml").write_text(
            """
schema_id: interval
inputs:
  design: {type: string, required: true, const: interval, aliases: [interval, 间比法]}
  ncols: {type: integer, required: true, aliases: [ncols, 列数, 田块列数], validation: {min: 1, max: 1000}}
  material_data: {type: artifact, required: true, source: {allowed: [artifact]}, aliases: [材料清单, material_data]}
  ck_spec: {type: string, required: true, aliases: [ck_spec, CK参数]}
""",
            encoding="utf-8",
        )
        (skill / "scripts" / "run.py").write_text(
            "import json, sys\npayload=json.load(sys.stdin)\nprint(json.dumps({'answer':'ok','selected':payload.get('_selected_schema_id'),'ncols':payload.get('ncols'),'artifact':payload.get('material_data')}, ensure_ascii=False))\n",
            encoding="utf-8",
        )
        return skill

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
        self.assertNotIn(SLOT_COLLECTION_FIELD, interrupts[0]["required_fields"])
        collection_ref = interrupts[0]["required_fields"][SLOT_COLLECTION_REF_FIELD]
        self.assertEqual(collection_ref["schema_version"], 2)
        self.assertEqual(collection_ref["selected_entrypoint"], "run")
        self.assertEqual(collection_ref["missing"], ["design"])
        collection = await self.runtime.storage.get_slot_collection(collection_ref["collection_id"])
        self.assertIsNotNone(collection)
        assert collection is not None
        self.assertEqual(collection.kind, "schema_selection")
        self.assertEqual(collection.missing, ("design",))
        events = await self.runtime.storage.list_slot_events(collection.collection_id)
        self.assertEqual([event.event_type for event in events], ["slot.collection_started", "slot.prompt_generated"])

    async def test_user_text_schema_match_is_not_overridden_by_upload_filename(self) -> None:
        root = self.workspace / "skill-v2-selector-source-precedence"
        self._write_field_design_upload_skill(root)
        await self.reconfigure_runtime(skill_roots=(root,), public_skill_roots=(root,), enable_skill_input_llm=False)
        upload = await self.client.post(
            "/api/v1/conversations/uploads",
            data={"conversation_id": "conv-v2-selector-source-precedence"},
            files={"file": ("interval_realistic_two_sets.csv", "ped_id,hyb_check,set\nA001,0,A\n", "text/csv")},
        )
        self.assertEqual(upload.status_code, 201, upload.text)

        response = await self.submit_message(
            conversation_id="conv-v2-selector-source-precedence",
            content="你帮我设计一个对角线增广试验",
            capability_id="skill.field_design",
            metadata={"upload_ids": [upload.json()["upload_id"]]},
        )
        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]
        await self.wait_for_condition(lambda: self.runtime.list_interrupts(task_id))
        interrupt = (await self.runtime.list_interrupts(task_id))[0]
        collection_ref = interrupt["required_fields"][SLOT_COLLECTION_REF_FIELD]
        collection = await self.runtime.storage.get_slot_collection(collection_ref["collection_id"])
        self.assertIsNotNone(collection)
        assert collection is not None

        self.assertEqual(collection.kind, "input_collection")
        self.assertEqual(collection.selected_schema_id, "diagonal")
        self.assertIn("ncols", collection.missing)
        self.assertNotIn("design", collection.missing)
        self.assertNotIn("material_data", collection.missing)

    async def test_v2_initial_skill_trigger_uses_llm_slot_resolver_for_user_sentence_parameters(self) -> None:
        root = self.workspace / "skill-v2-initial-llm-slot-resolver"
        self._write_field_design_upload_skill(root)
        seen_prompts: list[str] = []

        async def slot_generator(prompt: str, **_kwargs) -> str:
            if "v2 Skill 参数补槽器" in prompt:
                seen_prompts.append(prompt)
                return json.dumps(
                    {
                        "resolved": {
                            "ncols": {"raw_value": "田块10列", "value": 10, "source": "query"},
                            "ck_spec": {
                                "raw_value": "ck：1,2,8; 2,6,11; 3,1,9; 4,6,12",
                                "value": "1,2,8; 2,6,11; 3,1,9; 4,6,12",
                                "source": "query",
                            },
                        },
                        "missing": [],
                    },
                    ensure_ascii=False,
                )
            return "{}"

        await self.reconfigure_runtime(
            skill_roots=(root,),
            public_skill_roots=(root,),
            skill_input_text_generator=slot_generator,
            enable_skill_input_llm=True,
        )
        upload = await self.client.post(
            "/api/v1/conversations/uploads",
            data={"conversation_id": "conv-v2-initial-llm"},
            files={"file": ("interval_realistic_two_sets.csv", "ped_id,hyb_check,set\nA001,0,A\n", "text/csv")},
        )
        self.assertEqual(upload.status_code, 201, upload.text)

        response = await self.submit_message(
            conversation_id="conv-v2-initial-llm",
            content="你给我做一个间比法试验设计，田块10列，ck：1,2,8; 2,6,11; 3,1,9; 4,6,12",
            capability_id="skill.field_design",
            metadata={"upload_ids": [upload.json()["upload_id"]]},
        )
        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]
        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")
        self.assertEqual(await self.runtime.list_interrupts(task_id), [])

        collections = await self.runtime.storage.list_slot_collections_for_task(task_id)
        self.assertEqual(len(collections), 0)
        events = await self.runtime.storage.list_events_for_task(task_id)
        input_resolved = next(event for event in events if event.event_type == "skill.input_resolved")
        sources = input_resolved.payload["sources"]
        self.assertEqual(sources["ncols"]["source"], "llm_slot_resolver:query")
        self.assertEqual(sources["ck_spec"]["source"], "llm_slot_resolver:query")
        self.assertTrue(seen_prompts)
        self.assertIn("ck_spec", seen_prompts[0])
        self.assertIn("ncols", seen_prompts[0])

    async def test_v2_raw_answer_dto_extracts_canonical_values_and_keeps_chat_raw(self) -> None:
        root = self.workspace / "skill-v2-raw"
        self._write_diagonal_skill(root)

        async def slot_generator(prompt: str, **_kwargs) -> str:
            self.assertIn('"mode": "normal_extraction"', prompt)
            self.assertIn("对角线增广", prompt)
            return '{"resolved":{"ncols":{"raw_value":"12列","value":"12列","source":"current_answer"}}}'

        await self.reconfigure_runtime(
            skill_roots=(root,),
            public_skill_roots=(root,),
            skill_input_text_generator=slot_generator,
            enable_skill_input_llm=True,
        )
        response = await self.submit_message(
            conversation_id="conv-v2-raw",
            content="做对角线增广设计",
            capability_id="skill.field_design",
        )
        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]
        await self.wait_for_condition(lambda: self.runtime.list_interrupts(task_id))
        interrupt = (await self.runtime.list_interrupts(task_id))[0]
        collection_ref = interrupt["required_fields"][SLOT_COLLECTION_REF_FIELD]

        answer = await self.client.post(
            "/api/v1/tasks/interrupts/answer",
            json={
                "task_id": task_id,
                "interrupt_id": interrupt["interrupt_id"],
                "client_request_id": "req-v2-raw-1",
                "answer": {"text": "12列"},
            },
        )
        self.assertEqual(answer.status_code, 202, answer.text)
        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")

        collection = await self.runtime.storage.get_slot_collection(collection_ref["collection_id"])
        self.assertIsNotNone(collection)
        assert collection is not None
        self.assertEqual(collection.resolved["design"]["value"], "diagonal")
        self.assertEqual(collection.resolved["ncols"]["raw_value"], "12列")
        self.assertEqual(collection.resolved["ncols"]["value"], 12)
        events = await self.runtime.storage.list_slot_events(collection.collection_id)
        self.assertIn("slot.script_scheduled", [event.event_type for event in events])
        self.assertIn("slot.extraction_started", [event.event_type for event in events])
        self.assertIn("slot.validation_started", [event.event_type for event in events])

        messages = await self.runtime.storage.list_messages_for_conversation("conv-v2-raw")
        user_contents = [message.content for message in messages if str(message.role) == "user"]
        self.assertTrue(any(content == "12列" for content in user_contents), user_contents)
        self.assertFalse(any("design=对角线增广" in content for content in user_contents))

    async def test_v2_answer_merges_text_and_upload_in_same_round(self) -> None:
        root = self.workspace / "skill-v2-text-upload-merge"
        self._write_field_design_upload_skill(root)
        seen_prompts: list[str] = []

        async def slot_generator(prompt: str, **_kwargs) -> str:
            if '"mode": "normal_extraction"' in prompt:
                seen_prompts.append(prompt)
                return '{"resolved":{"ncols":{"raw_value":"田块12列","value":"12列","source":"current_answer"}}}'
            return "{}"

        await self.reconfigure_runtime(
            skill_roots=(root,),
            public_skill_roots=(root,),
            skill_input_text_generator=slot_generator,
            enable_skill_input_llm=True,
        )
        response = await self.submit_message(
            conversation_id="conv-v2-text-upload-merge",
            content="做对角线增广设计",
            capability_id="skill.field_design",
        )
        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]
        await self.wait_for_condition(lambda: self.runtime.list_interrupts(task_id))
        interrupt = (await self.runtime.list_interrupts(task_id))[0]
        collection_id = interrupt["required_fields"][SLOT_COLLECTION_REF_FIELD]["collection_id"]

        upload = await self.client.post(
            "/api/v1/conversations/uploads",
            data={"conversation_id": "conv-v2-text-upload-merge"},
            files={"file": ("interval_realistic_two_sets.csv", "ped_id,hyb_check,set\nA001,0,A\n", "text/csv")},
        )
        self.assertEqual(upload.status_code, 201, upload.text)
        upload_id = upload.json()["upload_id"]
        answer = await self.client.post(
            "/api/v1/tasks/interrupts/answer",
            json={
                "task_id": task_id,
                "interrupt_id": interrupt["interrupt_id"],
                "client_request_id": "req-v2-text-upload-merge",
                "answer": {"text": "田块12列", "upload_ids": [upload_id]},
            },
        )
        self.assertEqual(answer.status_code, 202, answer.text)
        await self.runtime._await_existing_execution(task_id)

        collection = await self.runtime.storage.get_slot_collection(collection_id)
        self.assertIsNotNone(collection)
        assert collection is not None
        self.assertEqual(collection.resolved["ncols"]["value"], 12)
        self.assertEqual(collection.resolved["material_data"]["source"], "task_attachment")
        self.assertEqual(collection.resolved["material_data"]["value"]["upload_ids"], [upload_id])
        self.assertNotIn("material_data", collection.missing)
        self.assertTrue(seen_prompts)
        prompt_payload = json.loads(seen_prompts[-1])
        self.assertEqual(prompt_payload["artifact_summaries"][0]["columns"], ["ped_id", "hyb_check", "set"])
        self.assertEqual(prompt_payload["artifact_summaries"][0]["row_count"], 1)
        self.assertEqual(prompt_payload["artifact_summaries"][0]["source_kind"], "interrupt_answer_upload")

    async def test_schema_selection_consumes_existing_task_attachment(self) -> None:
        root = self.workspace / "skill-v2-schema-selection-attachment-merge"
        self._write_field_design_upload_skill(root)
        await self.reconfigure_runtime(skill_roots=(root,), public_skill_roots=(root,), enable_skill_input_llm=False)
        upload = await self.client.post(
            "/api/v1/conversations/uploads",
            data={"conversation_id": "conv-v2-schema-selection-attachment-merge"},
            files={"file": ("materials.csv", "ped_id,hyb_check,set\nA001,0,A\n", "text/csv")},
        )
        self.assertEqual(upload.status_code, 201, upload.text)
        upload_id = upload.json()["upload_id"]
        response = await self.submit_message(
            conversation_id="conv-v2-schema-selection-attachment-merge",
            content="做田间试验设计",
            capability_id="skill.field_design",
            metadata={"upload_ids": [upload_id]},
        )
        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]
        await self.wait_for_condition(lambda: self.runtime.list_interrupts(task_id))
        interrupt = (await self.runtime.list_interrupts(task_id))[0]
        collection_id = interrupt["required_fields"][SLOT_COLLECTION_REF_FIELD]["collection_id"]
        collection = await self.runtime.storage.get_slot_collection(collection_id)
        self.assertIsNotNone(collection)
        assert collection is not None
        self.assertEqual(collection.kind, "schema_selection")

        answer = await self.client.post(
            "/api/v1/tasks/interrupts/answer",
            json={
                "task_id": task_id,
                "interrupt_id": interrupt["interrupt_id"],
                "client_request_id": "req-schema-select-attachment",
                "answer": {"text": "对角线增广"},
            },
        )
        self.assertEqual(answer.status_code, 202, answer.text)
        await self.runtime._await_existing_execution(task_id)
        selected = await self.runtime.storage.get_slot_collection(collection_id)
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.kind, "input_collection")
        self.assertEqual(selected.selected_schema_id, "diagonal")
        self.assertEqual(selected.resolved["material_data"]["source"], "task_attachment")
        self.assertEqual(selected.resolved["material_data"]["value"]["upload_ids"], [upload_id])
        self.assertIn("ncols", selected.missing)
        self.assertNotIn("material_data", selected.missing)

    async def test_v2_field_shaped_answer_payload_is_rejected(self) -> None:
        root = self.workspace / "skill-v2-reject"
        self._write_diagonal_skill(root)
        await self.reconfigure_runtime(skill_roots=(root,), public_skill_roots=(root,), enable_skill_input_llm=False)
        response = await self.submit_message(
            conversation_id="conv-v2-reject",
            content="做对角线增广设计",
            capability_id="skill.field_design",
        )
        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]
        await self.wait_for_condition(lambda: self.runtime.list_interrupts(task_id))
        interrupt = (await self.runtime.list_interrupts(task_id))[0]

        answer = await self.client.post(
            "/api/v1/tasks/interrupts/answer",
            json={
                "task_id": task_id,
                "interrupt_id": interrupt["interrupt_id"],
                "answer_payload": {"design": "对角线增广"},
            },
        )
        self.assertEqual(answer.status_code, 400, answer.text)

    async def test_schema_selection_answer_records_schema_selected_event(self) -> None:
        root = self.workspace / "skill-v2-schema-selection"
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
  interval: {path: schemas/interval.input.yaml, aliases: [interval, 间比法], entrypoint: run}
""", encoding="utf-8")
        (skill / "schemas" / "rcbd.input.yaml").write_text("schema_id: rcbd\ninputs: {design: {type: string, required: true, const: rcbd}}\n", encoding="utf-8")
        (skill / "schemas" / "interval.input.yaml").write_text("schema_id: interval\ninputs: {design: {type: string, required: true, const: interval}, ck_spec: {type: string, required: true}}\n", encoding="utf-8")
        (skill / "scripts" / "run.py").write_text("import json\nprint(json.dumps({'answer':'ok'}))\n", encoding="utf-8")
        await self.reconfigure_runtime(skill_roots=(root,), public_skill_roots=(root,), enable_skill_input_llm=False)
        response = await self.submit_message(content="做田间试验设计", capability_id="skill.design")
        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]
        await self.wait_for_condition(lambda: self.runtime.list_interrupts(task_id))
        interrupt = (await self.runtime.list_interrupts(task_id))[0]
        collection_id = interrupt["required_fields"][SLOT_COLLECTION_REF_FIELD]["collection_id"]

        answer = await self.client.post(
            "/api/v1/tasks/interrupts/answer",
            json={
                "task_id": task_id,
                "interrupt_id": interrupt["interrupt_id"],
                "client_request_id": "req-schema-select",
                "answer": {"text": "间比法"},
            },
        )
        self.assertEqual(answer.status_code, 202, answer.text)
        await self.runtime._await_existing_execution(task_id)
        collection = await self.runtime.storage.get_slot_collection(collection_id)
        self.assertIsNotNone(collection)
        assert collection is not None
        self.assertEqual(collection.kind, "input_collection")
        self.assertEqual(collection.selected_schema_id, "interval")
        self.assertEqual(collection.resolved["design"]["value"], "interval")
        self.assertNotIn("design", collection.missing)
        self.assertIn("ck_spec", collection.missing)
        events = await self.runtime.storage.list_slot_events(collection.collection_id)
        self.assertIn("slot.schema_selected", [event.event_type for event in events])

    async def test_ready_collection_without_script_event_is_recovered_by_scheduler_gate(self) -> None:
        root = self.workspace / "skill-v2-ready-recovery"
        self._write_diagonal_skill(root)
        await self.reconfigure_runtime(skill_roots=(root,), public_skill_roots=(root,), enable_skill_input_llm=False)
        response = await self.submit_message(
            conversation_id="conv-v2-ready-recovery",
            content="做对角线增广设计",
            capability_id="skill.field_design",
        )
        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]
        await self.wait_for_condition(lambda: self.runtime.list_interrupts(task_id))
        interrupt = (await self.runtime.list_interrupts(task_id))[0]
        collection_id = interrupt["required_fields"][SLOT_COLLECTION_REF_FIELD]["collection_id"]
        collection = await self.runtime.storage.get_slot_collection(collection_id)
        self.assertIsNotNone(collection)
        assert collection is not None
        await self.runtime.storage.save_slot_collection(
            replace(
                collection,
                status="ready",
                resolved={
                    "design": {"raw_value": "对角线增广", "value": "diagonal", "source": "schema_selection"},
                    "ncols": {"raw_value": "12列", "value": 12, "source": "current_answer"},
                },
                missing=(),
            )
        )

        await self.runtime.list_interrupts(task_id)
        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")
        events = await self.runtime.storage.list_slot_events(collection_id)
        self.assertEqual(len([event for event in events if event.event_type == "slot.script_scheduled"]), 1)

    async def test_history_recall_prompt_receives_previous_accepted_v2_answer_summaries(self) -> None:
        root = self.workspace / "skill-v2-history-runtime"
        self._write_diagonal_skill(root)
        seen_history_prompts: list[str] = []

        async def slot_generator(prompt: str, **_kwargs) -> str:
            if '"mode": "history_recall_extraction"' in prompt:
                seen_history_prompts.append(prompt)
                self.assertIn("还没有列数", prompt)
                return '{"resolved":{"ncols":{"raw_value":"12列","value":12,"source":"history"}}}'
            return "{}"

        await self.reconfigure_runtime(
            skill_roots=(root,),
            public_skill_roots=(root,),
            skill_input_text_generator=slot_generator,
            enable_skill_input_llm=True,
        )
        response = await self.submit_message(
            conversation_id="conv-v2-history-runtime",
            content="做对角线增广设计",
            capability_id="skill.field_design",
        )
        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]
        await self.wait_for_condition(lambda: self.runtime.list_interrupts(task_id))
        first_interrupt = (await self.runtime.list_interrupts(task_id))[0]
        first = await self.client.post(
            "/api/v1/tasks/interrupts/answer",
            json={
                "task_id": task_id,
                "interrupt_id": first_interrupt["interrupt_id"],
                "client_request_id": "req-history-first",
                "answer": {"text": "还没有列数"},
            },
        )
        self.assertEqual(first.status_code, 202, first.text)
        await self.runtime._await_existing_execution(task_id)
        second_interrupt = [
            interrupt for interrupt in await self.runtime.list_interrupts(task_id)
            if interrupt["status"] == "open" and interrupt["interrupt_id"] != first_interrupt["interrupt_id"]
        ][0]
        second = await self.client.post(
            "/api/v1/tasks/interrupts/answer",
            json={
                "task_id": task_id,
                "interrupt_id": second_interrupt["interrupt_id"],
                "client_request_id": "req-history-second",
                "answer": {"text": "我之前不是告诉过你了吗"},
            },
        )
        self.assertEqual(second.status_code, 202, second.text)
        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")
        self.assertTrue(seen_history_prompts)

    async def test_duplicate_v2_answer_is_idempotent_and_does_not_duplicate_events_or_messages(self) -> None:
        root = self.workspace / "skill-v2-idempotent"
        self._write_diagonal_skill(root)
        await self.reconfigure_runtime(skill_roots=(root,), public_skill_roots=(root,), enable_skill_input_llm=False)
        response = await self.submit_message(
            conversation_id="conv-v2-idempotent",
            content="做对角线增广设计",
            capability_id="skill.field_design",
        )
        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]
        await self.wait_for_condition(lambda: self.runtime.list_interrupts(task_id))
        interrupt = (await self.runtime.list_interrupts(task_id))[0]
        collection_ref = interrupt["required_fields"][SLOT_COLLECTION_REF_FIELD]
        body = {
            "task_id": task_id,
            "interrupt_id": interrupt["interrupt_id"],
            "client_request_id": "req-v2-idempotent",
            "answer": {"text": "12列"},
        }

        first = await self.client.post("/api/v1/tasks/interrupts/answer", json=body)
        self.assertEqual(first.status_code, 202, first.text)
        await self.wait_for_terminal_task(task_id)
        collection = await self.runtime.storage.get_slot_collection(collection_ref["collection_id"])
        self.assertIsNotNone(collection)
        assert collection is not None
        events_before = await self.runtime.storage.list_slot_events(collection.collection_id)
        messages_before = await self.runtime.storage.list_messages_for_conversation("conv-v2-idempotent")
        answers_before = await self.runtime.storage.list_interrupt_answers(interrupt["interrupt_id"])

        duplicate = await self.client.post("/api/v1/tasks/interrupts/answer", json=body)
        self.assertEqual(duplicate.status_code, 202, duplicate.text)
        events_after = await self.runtime.storage.list_slot_events(collection.collection_id)
        messages_after = await self.runtime.storage.list_messages_for_conversation("conv-v2-idempotent")
        answers_after = await self.runtime.storage.list_interrupt_answers(interrupt["interrupt_id"])

        self.assertEqual([event.event_type for event in events_after], [event.event_type for event in events_before])
        self.assertEqual(
            len([event for event in events_after if event.idempotency_key == f"answer:{interrupt['interrupt_id']}:req-v2-idempotent"]),
            1,
        )
        self.assertEqual(len([event for event in events_after if event.event_type == "slot.script_scheduled"]), 1)
        self.assertEqual(len(answers_after), len(answers_before))
        self.assertEqual(
            [message.content for message in messages_after if str(message.role) == "user"],
            [message.content for message in messages_before if str(message.role) == "user"],
        )

    async def test_old_embedded_v2_slot_collection_without_persisted_state_fails_closed(self) -> None:
        root = self.workspace / "skill-v2-old-embedded"
        self._write_diagonal_skill(root)
        await self.reconfigure_runtime(skill_roots=(root,), public_skill_roots=(root,), enable_skill_input_llm=False)
        response = await self.submit_message(
            conversation_id="conv-v2-old-embedded",
            content="做对角线增广设计",
            capability_id="skill.field_design",
        )
        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]
        await self.wait_for_condition(lambda: self.runtime.list_interrupts(task_id))
        interrupt_payload = (await self.runtime.list_interrupts(task_id))[0]
        interrupt = await self.runtime.storage.get_interrupt(interrupt_payload["interrupt_id"])
        self.assertIsNotNone(interrupt)
        assert interrupt is not None
        await self.runtime.storage.save_interrupt(
            replace(
                interrupt,
                required_fields={
                    SLOT_COLLECTION_FIELD: {
                        "schema_version": 2,
                        "collection_id": "missing-slot-collection",
                        "missing": ["ncols"],
                    }
                },
            )
        )

        answer = await self.client.post(
            "/api/v1/tasks/interrupts/answer",
            json={
                "task_id": task_id,
                "interrupt_id": interrupt.interrupt_id,
                "client_request_id": "req-v2-old-embedded",
                "answer": {"text": "12列"},
            },
        )
        self.assertEqual(answer.status_code, 400, answer.text)
        self.assertIn("slot collection state is missing", answer.text)

    async def test_cancel_marks_active_slot_collection_cancelled(self) -> None:
        root = self.workspace / "skill-v2-cancel"
        self._write_diagonal_skill(root)
        await self.reconfigure_runtime(skill_roots=(root,), public_skill_roots=(root,), enable_skill_input_llm=False)
        response = await self.submit_message(
            conversation_id="conv-v2-cancel",
            content="做对角线增广设计",
            capability_id="skill.field_design",
        )
        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]
        await self.wait_for_condition(lambda: self.runtime.list_interrupts(task_id))
        interrupt = (await self.runtime.list_interrupts(task_id))[0]
        collection_id = interrupt["required_fields"][SLOT_COLLECTION_REF_FIELD]["collection_id"]

        cancel = await self.client.post("/api/v1/tasks/cancel", json={"task_id": task_id})
        self.assertEqual(cancel.status_code, 202, cancel.text)
        collection = await self.runtime.storage.get_slot_collection(collection_id)
        self.assertIsNotNone(collection)
        assert collection is not None
        self.assertEqual(collection.status, "cancelled")
        events = await self.runtime.storage.list_slot_events(collection.collection_id)
        self.assertIn("slot.collection_cancelled", [event.event_type for event in events])

    async def test_waiting_collection_without_open_interrupt_is_recovered_for_frontend_polling(self) -> None:
        root = self.workspace / "skill-v2-recovery"
        self._write_diagonal_skill(root)
        await self.reconfigure_runtime(skill_roots=(root,), public_skill_roots=(root,), enable_skill_input_llm=False)
        response = await self.submit_message(
            conversation_id="conv-v2-recovery",
            content="做对角线增广设计",
            capability_id="skill.field_design",
        )
        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]
        await self.wait_for_condition(lambda: self.runtime.list_interrupts(task_id))
        interrupt_payload = (await self.runtime.list_interrupts(task_id))[0]
        collection_id = interrupt_payload["required_fields"][SLOT_COLLECTION_REF_FIELD]["collection_id"]
        interrupt = await self.runtime.storage.get_interrupt(interrupt_payload["interrupt_id"])
        self.assertIsNotNone(interrupt)
        assert interrupt is not None
        await self.runtime.storage.save_interrupt(replace(interrupt, status=InterruptStatus.CANCELLED))

        interrupts = await self.runtime.list_interrupts(task_id)
        open_recovered = [
            item for item in interrupts
            if item["status"] == "open" and item["required_fields"].get(SLOT_COLLECTION_REF_FIELD, {}).get("collection_id") == collection_id
        ]
        self.assertTrue(open_recovered, interrupts)
        events = await self.runtime.storage.list_slot_events(collection_id)
        self.assertIn("slot.interrupt_recovered", [event.event_type for event in events])
