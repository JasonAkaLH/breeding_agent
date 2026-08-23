from __future__ import annotations

import json
from dataclasses import replace

from src.core.enums import InterruptStatus
from src.core.models import SlotCollection
from src.integrations.agent_skills.missing_input_interrupt import SLOT_COLLECTION_FIELD, SLOT_COLLECTION_REF_FIELD
from tests.api.support import APITestCase


class SkillSlotCollectionV2APITest(APITestCase):
    def _write_diagonal_skill(self, root):
        skill = root / "field-design"
        (skill / "scripts").mkdir(parents=True)
        (skill / "schemas").mkdir()
        (skill / "references").mkdir()
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
        (skill / "references").mkdir()
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
resources:
  material_data_example: {path: references/material-data.md, audience: [main_agent, slot_question]}
""", encoding="utf-8")
        (skill / "references" / "material-data.md").write_text("材料数据示例列：ped_id, hyb_check, set。hyb_check=0 表示普通材料，1 表示检查种。", encoding="utf-8")
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

    def _write_runtime_missing_display_skill(self, root):
        skill = root / "runtime-display"
        (skill / "scripts").mkdir(parents=True)
        (skill / "schemas").mkdir()
        (skill / "SKILL.md").write_text(
            "---\nname: runtime-display\ndescription: runtime display\n---\n\n# Runtime Display\n",
            encoding="utf-8",
        )
        (skill / "skill.contract.yaml").write_text(
            """
contract_version: '2'
capability: {id: skill.runtime_display, display_name: Runtime Display}
runtime: {mode: python_subprocess, answer_mode: direct}
schema_selector: {strategy: deterministic_then_llm, selector_field: mode, default: display}
entrypoints: {run: {path: scripts/run.py}}
input_schemas:
  display: {path: schemas/display.input.yaml, aliases: [display, 动态展示], entrypoint: run}
""",
            encoding="utf-8",
        )
        (skill / "schemas" / "display.input.yaml").write_text(
            """
schema_id: display
inputs:
  mode: {type: string, required: true, const: display, aliases: [display, 动态展示]}
  choice:
    type: string
    required: false
    aliases: [choice, 选择]
    patterns:
      - '(?:choice|选择)\\s*[:：=]?\\s*(\\d+)'
      - '^\\s*(\\d+)\\s*$'
""",
            encoding="utf-8",
        )
        (skill / "scripts" / "run.py").write_text(
            "\n".join(
                [
                    "import json, sys",
                    "payload=json.load(sys.stdin)",
                    "choice=payload.get('choice')",
                    "if choice:",
                    "    print(json.dumps({'answer': '完成 choice=' + str(choice), 'choice': choice}, ensure_ascii=False))",
                    "else:",
                    "    print(json.dumps({",
                    "        'ok': False,",
                    "        'is_error': True,",
                    "        'error': {'type': 'missing_input', 'message': '请选择候选项。'},",
                    "        'missing': ['choice'],",
                    "        'answer': '识别到 2 个候选项。请根据下表补充 choice。',",
                    "        'columns': ['编号', '名称'],",
                    "        'rows': [{'编号': 1, '名称': '候选A'}, {'编号': 2, '名称': '候选B'}],",
                    "    }, ensure_ascii=False))",
                    "",
                ]
            ),
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

        async def slot_generator(prompt: str, **kwargs) -> str:
            if '"mode": "interrupt_turn_understanding"' in prompt:
                self.assertEqual(kwargs.get("metadata", {}).get("main_agent_reasoning_effort"), "minimal")
                return json.dumps({"intent": "slot_answer", "confidence": 0.96, "reason": "numeric slot value"}, ensure_ascii=False)
            if '"mode": "interrupt_resume_verification"' in prompt:
                self.assertEqual(kwargs.get("metadata", {}).get("main_agent_reasoning_effort"), "minimal")
                return json.dumps({"allow_resume": True, "confidence": 0.98, "reason": "numeric slot value"}, ensure_ascii=False)
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

        answer = await self.answer_interrupt_with_chat(
            conversation_id="conv-v2-raw",
            interrupt_id=interrupt["interrupt_id"],
            client_message_id="req-v2-raw-1",
            content="12列",
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

    async def test_interrupt_open_slot_answer_hint_reaches_extraction_and_resolves_bare_scalar(self) -> None:
        root = self.workspace / "skill-v2-planner-hint"
        self._write_diagonal_skill(root)
        seen: dict[str, object] = {}

        async def slot_generator(prompt: str, **_kwargs) -> str:
            payload = json.loads(prompt)
            mode = payload.get("mode")
            if mode == "interrupt_turn_understanding":
                return json.dumps(
                    {
                        "parts": [
                            {
                                "part_id": "slot-ncols",
                                "kind": "slot_answer",
                                "text": "10",
                                "target_slots": ["ncols"],
                                "confidence": 0.99,
                                "reason": "用户明确提供了田块列数10，对应缺失的ncols槽位。",
                            }
                        ],
                        "confidence": 0.99,
                        "reason": "clear slot answer",
                    },
                    ensure_ascii=False,
                )
            if mode == "interrupt_resume_verification":
                return json.dumps({"allow_resume": True, "confidence": 0.99, "reason": "clear slot answer"}, ensure_ascii=False)
            if mode == "normal_extraction":
                seen["turn_hint"] = payload.get("turn_hint")
                seen["current_user_answer"] = payload.get("current_user_answer")
                return "{}"
            return "{}"

        await self.reconfigure_runtime(
            skill_roots=(root,),
            public_skill_roots=(root,),
            skill_input_text_generator=slot_generator,
            enable_skill_input_llm=True,
        )
        response = await self.submit_message(
            conversation_id="conv-v2-planner-hint",
            content="做对角线增广设计",
            capability_id="skill.field_design",
        )
        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]
        await self.wait_for_condition(lambda: self.runtime.list_interrupts(task_id))
        interrupt = (await self.runtime.list_interrupts(task_id))[0]
        collection_id = interrupt["required_fields"][SLOT_COLLECTION_REF_FIELD]["collection_id"]

        answer = await self.client.post(
            "/api/v1/conversations/chat-messages",
            json={
                "conversation_id": "conv-v2-planner-hint",
                "content": "10",
                "routing_mode": "auto",
                "capability_id": None,
                "client_message_id": "client-planner-hint",
                "metadata": {"interrupt_id": interrupt["interrupt_id"]},
            },
        )
        self.assertEqual(answer.status_code, 202, answer.text)
        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")

        self.assertEqual(seen["current_user_answer"], "10")
        turn_hint = seen["turn_hint"]
        self.assertIsInstance(turn_hint, dict)
        assert isinstance(turn_hint, dict)
        self.assertEqual(turn_hint["target_slots"], ["ncols"])
        self.assertEqual(turn_hint["reason"], "用户明确提供了田块列数10，对应缺失的ncols槽位。")
        collection = await self.runtime.storage.get_slot_collection(collection_id)
        self.assertIsNotNone(collection)
        assert collection is not None
        self.assertEqual(collection.resolved["ncols"]["value"], 10)
        self.assertEqual(collection.resolved["ncols"]["source"], "turn_hint")

    async def test_v2_interrupt_question_is_persisted_as_visible_assistant_history(self) -> None:
        root = self.workspace / "skill-v2-visible-question"
        self._write_diagonal_skill(root)
        await self.reconfigure_runtime(skill_roots=(root,), public_skill_roots=(root,), enable_skill_input_llm=False)

        response = await self.submit_message(
            conversation_id="conv-v2-visible-question",
            content="做对角线增广设计",
            capability_id="skill.field_design",
        )
        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]
        await self.wait_for_condition(lambda: self.runtime.list_interrupts(task_id))
        interrupt = (await self.runtime.list_interrupts(task_id))[0]

        messages = await self.runtime.storage.list_messages_for_conversation("conv-v2-visible-question")
        assistant_questions = [
            message
            for message in messages
            if str(message.role) == "assistant" and message.stream_status == "interrupt_visible"
        ]
        self.assertEqual([message.content for message in assistant_questions], [interrupt["question"]])

        history = await self.client.get("/api/v1/conversations/conv-v2-visible-question/messages")
        self.assertEqual(history.status_code, 200, history.text)
        history_messages = history.json()["messages"]
        self.assertTrue(
            any(
                message["role"] == "assistant"
                and message["content"] == interrupt["question"]
                and message["stream_status"] == "interrupt_visible"
                for message in history_messages
            )
        )

    async def test_runtime_missing_input_display_question_persists_and_resumes(self) -> None:
        root = self.workspace / "skill-v2-runtime-missing-display"
        self._write_runtime_missing_display_skill(root)
        await self.reconfigure_runtime(skill_roots=(root,), public_skill_roots=(root,), enable_skill_input_llm=False)

        response = await self.submit_message(
            conversation_id="conv-v2-runtime-missing-display",
            content="动态展示",
            capability_id="skill.runtime_display",
        )
        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]
        await self.wait_for_condition(lambda: self.runtime.list_interrupts(task_id))
        interrupt = (await self.runtime.list_interrupts(task_id))[0]
        question = interrupt["question"]

        self.assertIn("识别到 2 个候选项", question)
        self.assertIn("| 编号 | 名称 |", question)
        self.assertIn("| 1 | 候选A |", question)
        collection_ref = interrupt["required_fields"][SLOT_COLLECTION_REF_FIELD]
        collection_id = collection_ref["collection_id"]
        self.assertEqual(collection_ref["missing"], ["choice"])

        collection = await self.runtime.storage.get_slot_collection(collection_id)
        self.assertIsNotNone(collection)
        assert collection is not None
        self.assertEqual(collection.last_question, question)

        messages = await self.runtime.storage.list_messages_for_conversation("conv-v2-runtime-missing-display")
        assistant_questions = [
            message
            for message in messages
            if str(message.role) == "assistant" and message.stream_status == "interrupt_visible"
        ]
        self.assertEqual([message.content for message in assistant_questions], [question])

        history = await self.client.get("/api/v1/conversations/conv-v2-runtime-missing-display/messages")
        self.assertEqual(history.status_code, 200, history.text)
        history_messages = history.json()["messages"]
        self.assertTrue(
            any(
                message["role"] == "assistant"
                and message["content"] == question
                and message["stream_status"] == "interrupt_visible"
                for message in history_messages
            )
        )

        answer = await self.answer_interrupt_with_chat(
            conversation_id="conv-v2-runtime-missing-display",
            interrupt_id=interrupt["interrupt_id"],
            client_message_id="req-runtime-display-choice",
            content="1",
        )
        self.assertEqual(answer.status_code, 202, answer.text)
        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")
        collection_after = await self.runtime.storage.get_slot_collection(collection_id)
        self.assertIsNotNone(collection_after)
        assert collection_after is not None
        self.assertEqual(collection_after.resolved["choice"]["value"], "1")
        events = await self.runtime.storage.list_slot_events(collection_id)
        self.assertIn("slot.script_scheduled", [event.event_type for event in events])

    async def test_v2_interrupt_clarification_keeps_interrupt_open_and_does_not_resume(self) -> None:
        root = self.workspace / "skill-v2-clarification"
        self._write_diagonal_skill(root)

        async def slot_generator(prompt: str, **_kwargs) -> str:
            if '"mode": "interrupt_turn_understanding"' in prompt:
                return json.dumps(
                    {
                        "intent": "clarification_question",
                        "confidence": 0.98,
                        "reason": "asks for data format",
                        "clarification_answer": "列数请填写田块布局的总列数，例如 12 列。interrupt 会继续等待你的正式答案。",
                    },
                    ensure_ascii=False,
                )
            raise AssertionError(f"unexpected prompt: {prompt[:120]}")

        await self.reconfigure_runtime(
            skill_roots=(root,),
            public_skill_roots=(root,),
            skill_input_text_generator=slot_generator,
            enable_skill_input_llm=True,
        )
        response = await self.submit_message(
            conversation_id="conv-v2-clarification",
            content="做对角线增广设计",
            capability_id="skill.field_design",
        )
        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]
        await self.wait_for_condition(lambda: self.runtime.list_interrupts(task_id))
        interrupt = (await self.runtime.list_interrupts(task_id))[0]
        collection_id = interrupt["required_fields"][SLOT_COLLECTION_REF_FIELD]["collection_id"]

        answer = await self.answer_interrupt_with_chat(
            conversation_id="conv-v2-clarification",
            interrupt_id=interrupt["interrupt_id"],
            client_message_id="req-v2-clarify-format",
            content="这个列数应该填什么格式？",
        )
        self.assertEqual(answer.status_code, 202, answer.text)
        payload = answer.json()
        self.assertEqual(payload["action"], "interrupt_clarification_answer")
        self.assertEqual(payload["status"], "accepted")
        self.assertIn("12 列", payload["assistant_message"])

        persisted_interrupt = await self.runtime.storage.get_interrupt(interrupt["interrupt_id"])
        self.assertIsNotNone(persisted_interrupt)
        assert persisted_interrupt is not None
        self.assertEqual(persisted_interrupt.status, InterruptStatus.OPEN)
        self.assertEqual(await self.runtime.storage.list_interrupt_answers(interrupt["interrupt_id"]), [])
        node = await self.runtime.storage.get_task_node(interrupt["node_id"])
        self.assertIsNotNone(node)
        assert node is not None
        self.assertEqual(str(node.status), "waiting_for_input")
        collection = await self.runtime.storage.get_slot_collection(collection_id)
        self.assertIsNotNone(collection)
        assert collection is not None
        self.assertEqual(collection.status, "waiting_for_user")
        events = await self.runtime.storage.list_slot_events(collection.collection_id)
        self.assertIn("slot.clarification_answered", [event.event_type for event in events])
        task_events = await self.runtime.storage.list_events_for_task(task_id)
        self.assertIn("task.interrupt_clarification_answered", [event.event_type for event in task_events])
        self.assertNotIn("node.ready_to_resume", [event.event_type for event in task_events])
        messages = await self.runtime.storage.list_messages_for_conversation("conv-v2-clarification")
        self.assertTrue(any(message.content.startswith("这个列数") for message in messages if str(message.role) == "user"))
        self.assertTrue(any("interrupt 会继续等待" in message.content for message in messages if str(message.role) == "assistant"))

    async def test_chat_message_clarification_keeps_v2_interrupt_open(self) -> None:
        root = self.workspace / "skill-v2-chat-clarification"
        self._write_diagonal_skill(root)

        async def slot_generator(prompt: str, **kwargs) -> str:
            if '"mode": "interrupt_turn_understanding"' in prompt:
                self.assertEqual(kwargs.get("metadata", {}).get("main_agent_reasoning_effort"), "minimal")
                return json.dumps(
                    {
                        "intent": "clarification_question",
                        "confidence": 0.98,
                        "reason": "asks for data format",
                        "clarification_answer": "列数请填写田块布局的总列数，例如 12 列。interrupt 会继续等待你的正式答案。",
                    },
                    ensure_ascii=False,
                )
            raise AssertionError(f"unexpected prompt: {prompt[:120]}")

        await self.reconfigure_runtime(
            skill_roots=(root,),
            public_skill_roots=(root,),
            skill_input_text_generator=slot_generator,
            enable_skill_input_llm=True,
        )
        response = await self.submit_message(
            conversation_id="conv-v2-chat-clarification",
            content="做对角线增广设计",
            capability_id="skill.field_design",
        )
        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]
        await self.wait_for_condition(lambda: self.runtime.list_interrupts(task_id))
        interrupt = (await self.runtime.list_interrupts(task_id))[0]

        answer = await self.client.post(
            "/api/v1/conversations/chat-messages",
            json={
                "conversation_id": "conv-v2-chat-clarification",
                "content": "这个列数应该填什么格式？",
                "routing_mode": "auto",
                "capability_id": None,
                "client_message_id": "client-v2-chat-clarify-1",
                "metadata": {"interrupt_id": interrupt["interrupt_id"]},
            },
        )
        self.assertEqual(answer.status_code, 202, answer.text)
        payload = answer.json()
        self.assertEqual(payload["task_id"], task_id)
        self.assertEqual(payload["message_id"], "client-v2-chat-clarify-1")
        self.assertEqual(payload["action"], "interrupt_clarification_answer")
        self.assertEqual(payload["interrupt_id"], interrupt["interrupt_id"])
        self.assertIn("12 列", payload["assistant_message"])

        persisted_interrupt = await self.runtime.storage.get_interrupt(interrupt["interrupt_id"])
        self.assertIsNotNone(persisted_interrupt)
        assert persisted_interrupt is not None
        self.assertEqual(persisted_interrupt.status, InterruptStatus.OPEN)
        self.assertEqual(await self.runtime.storage.list_interrupt_answers(interrupt["interrupt_id"]), [])
        messages = await self.runtime.storage.list_messages_for_conversation("conv-v2-chat-clarification")
        self.assertTrue(any(message.message_id == "client-v2-chat-clarify-1" for message in messages))

    async def test_v2_interrupt_invalid_understanding_response_keeps_interrupt_open(self) -> None:
        root = self.workspace / "skill-v2-invalid-understanding"
        self._write_diagonal_skill(root)

        async def slot_generator(prompt: str, **kwargs) -> str:
            if '"mode": "interrupt_turn_understanding"' in prompt:
                self.assertEqual(kwargs.get("metadata", {}).get("main_agent_reasoning_effort"), "minimal")
                return "not-json"
            if '"mode": "interrupt_clarification_answer"' in prompt:
                self.assertEqual(kwargs.get("metadata", {}).get("main_agent_reasoning_effort"), "minimal")
                return json.dumps({"answer": "我还不能确认这是正式答案，当前 interrupt 会继续保持打开。请直接回复列数，例如 12 列。"}, ensure_ascii=False)
            raise AssertionError(f"unexpected prompt: {prompt[:120]}")

        await self.reconfigure_runtime(
            skill_roots=(root,),
            public_skill_roots=(root,),
            skill_input_text_generator=slot_generator,
            enable_skill_input_llm=True,
        )
        response = await self.submit_message(
            conversation_id="conv-v2-invalid-understanding",
            content="做对角线增广设计",
            capability_id="skill.field_design",
        )
        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]
        await self.wait_for_condition(lambda: self.runtime.list_interrupts(task_id))
        interrupt = (await self.runtime.list_interrupts(task_id))[0]

        answer = await self.answer_interrupt_with_chat(
            conversation_id="conv-v2-invalid-understanding",
            interrupt_id=interrupt["interrupt_id"],
            client_message_id="req-v2-invalid-understanding",
            content="12列？",
        )
        self.assertEqual(answer.status_code, 202, answer.text)
        payload = answer.json()
        self.assertEqual(payload["action"], "interrupt_clarification_answer")
        self.assertEqual(payload["status"], "accepted")
        self.assertIn("继续保持打开", payload["assistant_message"])
        self.assertEqual(await self.runtime.storage.list_interrupt_answers(interrupt["interrupt_id"]), [])

    async def test_v2_interrupt_invalid_resume_verifier_keeps_interrupt_open(self) -> None:
        root = self.workspace / "skill-v2-invalid-verifier"
        self._write_diagonal_skill(root)

        async def slot_generator(prompt: str, **_kwargs) -> str:
            if '"mode": "interrupt_turn_understanding"' in prompt:
                return json.dumps({"intent": "slot_answer", "confidence": 0.96, "reason": "looks numeric"}, ensure_ascii=False)
            if '"mode": "interrupt_resume_verification"' in prompt:
                return "not-json"
            if '"mode": "interrupt_clarification_answer"' in prompt:
                return json.dumps({"answer": "我还不能安全确认这是最终补槽答案，interrupt 会继续等待。请直接回复列数，例如 12 列。"}, ensure_ascii=False)
            raise AssertionError(f"unexpected prompt: {prompt[:120]}")

        await self.reconfigure_runtime(
            skill_roots=(root,),
            public_skill_roots=(root,),
            skill_input_text_generator=slot_generator,
            enable_skill_input_llm=True,
        )
        response = await self.submit_message(
            conversation_id="conv-v2-invalid-verifier",
            content="做对角线增广设计",
            capability_id="skill.field_design",
        )
        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]
        await self.wait_for_condition(lambda: self.runtime.list_interrupts(task_id))
        interrupt = (await self.runtime.list_interrupts(task_id))[0]

        answer = await self.answer_interrupt_with_chat(
            conversation_id="conv-v2-invalid-verifier",
            interrupt_id=interrupt["interrupt_id"],
            client_message_id="req-v2-invalid-verifier",
            content="12列",
        )
        self.assertEqual(answer.status_code, 202, answer.text)
        payload = answer.json()
        self.assertEqual(payload["action"], "interrupt_clarification_answer")
        self.assertEqual(payload["status"], "accepted")
        self.assertIn("继续等待", payload["assistant_message"])
        self.assertEqual(await self.runtime.storage.list_interrupt_answers(interrupt["interrupt_id"]), [])

    async def test_v2_answer_merges_text_and_upload_in_same_round(self) -> None:
        root = self.workspace / "skill-v2-text-upload-merge"
        self._write_field_design_upload_skill(root)
        seen_prompts: list[str] = []

        async def slot_generator(prompt: str, **_kwargs) -> str:
            if '"mode": "interrupt_turn_understanding"' in prompt:
                return json.dumps({"intent": "slot_answer", "confidence": 0.96, "reason": "text plus upload"}, ensure_ascii=False)
            if '"mode": "interrupt_resume_verification"' in prompt:
                return json.dumps({"allow_resume": True, "confidence": 0.98, "reason": "text plus upload"}, ensure_ascii=False)
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
        answer = await self.answer_interrupt_with_chat(
            conversation_id="conv-v2-text-upload-merge",
            interrupt_id=interrupt["interrupt_id"],
            client_message_id="req-v2-text-upload-merge",
            content="田块12列",
            metadata={"upload_ids": [upload_id]},
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

    async def test_interrupt_open_skill_question_uses_resource_context_and_keeps_interrupt_open(self) -> None:
        root = self.workspace / "skill-v2-resource-question"
        self._write_field_design_upload_skill(root)
        resource_prompts: list[dict] = []

        async def slot_generator(prompt: str, **_kwargs) -> str:
            if '"mode": "interrupt_turn_understanding"' in prompt:
                return json.dumps(
                    {
                        "parts": [
                            {
                                "part_id": "q1",
                                "kind": "skill_question",
                                "text": "能给我材料数据示例吗？",
                                "confidence": 0.99,
                                "reason": "asks for current skill data example",
                            }
                        ],
                        "confidence": 0.99,
                    },
                    ensure_ascii=False,
                )
            if '"mode": "interrupt_skill_question_answer"' in prompt:
                payload = json.loads(prompt)
                resource_prompts.append(payload)
                self.assertIn("ped_id", json.dumps(payload["resource_context"], ensure_ascii=False))
                return json.dumps({"answer": "材料数据示例列包括 ped_id、hyb_check、set。"}, ensure_ascii=False)
            return "{}"

        await self.reconfigure_runtime(
            skill_roots=(root,),
            public_skill_roots=(root,),
            skill_input_text_generator=slot_generator,
            enable_skill_input_llm=True,
        )
        response = await self.submit_message(
            conversation_id="conv-v2-resource-question",
            content="做对角线增广设计",
            capability_id="skill.field_design",
        )
        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]
        await self.wait_for_condition(lambda: self.runtime.list_interrupts(task_id))
        interrupt = (await self.runtime.list_interrupts(task_id))[0]

        answer = await self.client.post(
            "/api/v1/conversations/chat-messages",
            json={
                "conversation_id": "conv-v2-resource-question",
                "content": "能给我材料数据示例吗？",
                "routing_mode": "auto",
                "capability_id": None,
                "client_message_id": "client-resource-question",
                "metadata": {"interrupt_id": interrupt["interrupt_id"]},
            },
        )
        self.assertEqual(answer.status_code, 202, answer.text)
        payload = answer.json()
        self.assertEqual(payload["action"], "interrupt_clarification_answer")
        self.assertIn("ped_id", payload["assistant_message"])
        persisted = await self.runtime.storage.get_interrupt(interrupt["interrupt_id"])
        self.assertIsNotNone(persisted)
        assert persisted is not None
        self.assertEqual(persisted.status, InterruptStatus.OPEN)
        self.assertTrue(resource_prompts)
        events = await self.runtime.storage.list_events_for_task(task_id)
        event_types = [event.event_type for event in events]
        self.assertIn("task.interrupt_question_answered", event_types)
        self.assertIn("skill.question_answered", event_types)
        self.assertIn("skill.resource_read", event_types)
        resource_read_payloads = [event.payload for event in events if event.event_type == "skill.resource_read"]
        self.assertTrue(any(payload.get("resource_id") == "material_data_example" for payload in resource_read_payloads))
        self.assertNotIn("agent.final_output", event_types)

    async def test_interrupt_open_skill_question_includes_skill_md_overview(self) -> None:
        root = self.workspace / "skill-v2-skill-md-question"
        skill = self._write_field_design_upload_skill(root)
        (skill / "SKILL.md").write_text(
            "---\nname: field-design\ndescription: design\n---\n\n"
            "# Design Overview\n\n"
            "总纲规则：材料示例必须使用 `hyb_check = 0` 表示普通试验材料，非零值表示 checks；不要回答成 Y/N。\n",
            encoding="utf-8",
        )
        (skill / "references" / "material-data.md").write_text(
            "材料数据示例列：ped_id, hyb_check, set。",
            encoding="utf-8",
        )
        seen_prompts: list[dict] = []

        async def slot_generator(prompt: str, **_kwargs) -> str:
            if '"mode": "interrupt_turn_understanding"' in prompt:
                return json.dumps(
                    {
                        "parts": [
                            {
                                "part_id": "q1",
                                "kind": "skill_question",
                                "text": "能给我材料数据示例吗？",
                                "confidence": 0.99,
                                "reason": "asks for current skill data example",
                            }
                        ],
                        "confidence": 0.99,
                    },
                    ensure_ascii=False,
                )
            if '"mode": "interrupt_skill_question_answer"' in prompt:
                payload = json.loads(prompt)
                seen_prompts.append(payload)
                self.assertTrue(any("Skill 文档事实约束" in item for item in payload["instructions"]))
                self.assertTrue(any("不得用通用领域常识替换文档口径" in item for item in payload["instructions"]))
                resource_context = payload["resource_context"]
                self.assertEqual(resource_context[0]["resource_id"], "skill_overview")
                self.assertEqual(resource_context[0]["path"], "SKILL.md")
                self.assertIn("hyb_check = 0", resource_context[0]["content"])
                self.assertIn("不要回答成 Y/N", resource_context[0]["content"])
                return json.dumps({"answer": "示例应使用 hyb_check=0 表示普通材料，1 表示 check。"}, ensure_ascii=False)
            return "{}"

        await self.reconfigure_runtime(
            skill_roots=(root,),
            public_skill_roots=(root,),
            skill_input_text_generator=slot_generator,
            enable_skill_input_llm=True,
        )
        response = await self.submit_message(
            conversation_id="conv-v2-skill-md-question",
            content="做对角线增广设计",
            capability_id="skill.field_design",
        )
        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]
        await self.wait_for_condition(lambda: self.runtime.list_interrupts(task_id))
        interrupt = (await self.runtime.list_interrupts(task_id))[0]

        answer = await self.client.post(
            "/api/v1/conversations/chat-messages",
            json={
                "conversation_id": "conv-v2-skill-md-question",
                "content": "能给我材料数据示例吗？",
                "routing_mode": "auto",
                "capability_id": None,
                "client_message_id": "client-skill-md-question",
                "metadata": {"interrupt_id": interrupt["interrupt_id"]},
            },
        )
        self.assertEqual(answer.status_code, 202, answer.text)
        payload = answer.json()
        self.assertEqual(payload["action"], "interrupt_clarification_answer")
        self.assertIn("hyb_check=0", payload["assistant_message"])
        self.assertTrue(seen_prompts)
        events = await self.runtime.storage.list_events_for_task(task_id)
        resource_read_payloads = [event.payload for event in events if event.event_type == "skill.resource_read"]
        self.assertTrue(
            any(
                payload.get("resource_id") == "skill_overview" and payload.get("path") == "SKILL.md"
                for payload in resource_read_payloads
            )
        )

    async def test_interrupt_open_mixed_turn_updates_slot_answers_question_and_replays_summary(self) -> None:
        root = self.workspace / "skill-v2-mixed-idempotent"
        self._write_diagonal_skill(root)
        counts = {"planner": 0, "answer": 0, "extract": 0}

        async def slot_generator(prompt: str, **_kwargs) -> str:
            if '"mode": "interrupt_turn_understanding"' in prompt:
                counts["planner"] += 1
                return json.dumps(
                    {
                        "parts": [
                            {"part_id": "s1", "kind": "slot_answer", "text": "12列", "confidence": 0.96},
                            {"part_id": "q1", "kind": "skill_question", "text": "列数是什么意思？", "confidence": 0.95},
                        ],
                        "confidence": 0.97,
                    },
                    ensure_ascii=False,
                )
            if '"mode": "interrupt_skill_question_answer"' in prompt:
                counts["answer"] += 1
                return json.dumps({"answer": "列数是田块布局的总列数。"}, ensure_ascii=False)
            if '"mode": "normal_extraction"' in prompt:
                counts["extract"] += 1
                return '{"resolved":{"ncols":{"raw_value":"12列","value":12,"source":"current_answer"}}}'
            return "{}"

        await self.reconfigure_runtime(
            skill_roots=(root,),
            public_skill_roots=(root,),
            skill_input_text_generator=slot_generator,
            enable_skill_input_llm=True,
        )
        response = await self.submit_message(
            conversation_id="conv-v2-mixed-idempotent",
            content="做对角线增广设计",
            capability_id="skill.field_design",
        )
        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]
        await self.wait_for_condition(lambda: self.runtime.list_interrupts(task_id))
        interrupt = (await self.runtime.list_interrupts(task_id))[0]
        body = {
            "conversation_id": "conv-v2-mixed-idempotent",
            "content": "12列，列数是什么意思？",
            "routing_mode": "auto",
            "capability_id": None,
            "client_message_id": "client-mixed-once",
            "metadata": {"interrupt_id": interrupt["interrupt_id"]},
        }

        first = await self.client.post("/api/v1/conversations/chat-messages", json=body)
        self.assertEqual(first.status_code, 202, first.text)
        second = await self.client.post("/api/v1/conversations/chat-messages", json=body)
        self.assertEqual(second.status_code, 202, second.text)
        self.assertEqual(first.json()["assistant_message"], second.json()["assistant_message"])
        self.assertEqual(counts, {"planner": 1, "answer": 1, "extract": 1})
        collection_id = interrupt["required_fields"][SLOT_COLLECTION_REF_FIELD]["collection_id"]
        collection = await self.runtime.storage.get_slot_collection(collection_id)
        self.assertIsNotNone(collection)
        assert collection is not None
        self.assertEqual(collection.resolved["ncols"]["value"], 12)
        events = await self.runtime.storage.list_slot_events(collection_id)
        self.assertEqual(
            len([event for event in events if event.idempotency_key == f"interrupt_turn:{interrupt['interrupt_id']}:client-mixed-once"]),
            1,
        )
        self.assertEqual(len([event for event in events if event.event_type == "slot.script_scheduled"]), 1)

    async def test_interrupt_open_off_topic_guidance_keeps_interrupt_open_without_new_task(self) -> None:
        root = self.workspace / "skill-v2-off-topic"
        self._write_diagonal_skill(root)

        async def slot_generator(prompt: str, **_kwargs) -> str:
            if '"mode": "interrupt_turn_understanding"' in prompt:
                return json.dumps(
                    {
                        "parts": [
                            {"part_id": "o1", "kind": "off_topic_guidance", "text": "北京天气怎么样？", "confidence": 0.95}
                        ],
                        "confidence": 0.95,
                    },
                    ensure_ascii=False,
                )
            raise AssertionError(f"unexpected prompt: {prompt[:120]}")

        await self.reconfigure_runtime(
            skill_roots=(root,),
            public_skill_roots=(root,),
            skill_input_text_generator=slot_generator,
            enable_skill_input_llm=True,
        )
        response = await self.submit_message(
            conversation_id="conv-v2-off-topic",
            content="做对角线增广设计",
            capability_id="skill.field_design",
        )
        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]
        await self.wait_for_condition(lambda: self.runtime.list_interrupts(task_id))
        interrupt = (await self.runtime.list_interrupts(task_id))[0]
        before_tasks = await self.runtime.storage.list_tasks_for_conversation("conv-v2-off-topic")

        answer = await self.client.post(
            "/api/v1/conversations/chat-messages",
            json={
                "conversation_id": "conv-v2-off-topic",
                "content": "北京天气怎么样？",
                "routing_mode": "auto",
                "capability_id": None,
                "client_message_id": "client-off-topic",
                "metadata": {"interrupt_id": interrupt["interrupt_id"]},
            },
        )
        self.assertEqual(answer.status_code, 202, answer.text)
        payload = answer.json()
        self.assertEqual(payload["action"], "interrupt_clarification_answer")
        self.assertIn("interrupt", payload["assistant_message"])
        persisted = await self.runtime.storage.get_interrupt(interrupt["interrupt_id"])
        self.assertIsNotNone(persisted)
        assert persisted is not None
        self.assertEqual(persisted.status, InterruptStatus.OPEN)
        after_tasks = await self.runtime.storage.list_tasks_for_conversation("conv-v2-off-topic")
        self.assertEqual([task.task_id for task in after_tasks], [task.task_id for task in before_tasks])

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

        answer = await self.answer_interrupt_with_chat(
            conversation_id="conv-v2-schema-selection-attachment-merge",
            interrupt_id=interrupt["interrupt_id"],
            client_message_id="req-schema-select-attachment",
            content="对角线增广",
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

    async def test_interrupt_open_schema_switch_unspecified_reuse_keeps_confirmation_open(self) -> None:
        root = self.workspace / "skill-v2-schema-switch-unspecified"
        self._write_field_design_upload_skill(root)

        async def slot_generator(prompt: str, **_kwargs) -> str:
            if "v2 Skill 参数补槽器" in prompt:
                return json.dumps(
                    {"resolved": {"ncols": {"raw_value": "田块10列", "value": 10, "source": "query"}}, "missing": []},
                    ensure_ascii=False,
                )
            if '"mode": "interrupt_turn_understanding"' in prompt:
                return json.dumps(
                    {
                        "parts": [
                            {
                                "part_id": "switch",
                                "kind": "schema_switch",
                                "text": "改成对角线增广",
                                "target_schema_id": "diagonal",
                                "reuse_decision": "unspecified",
                                "confidence": 0.98,
                            }
                        ],
                        "confidence": 0.98,
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
            data={"conversation_id": "conv-v2-schema-switch-unspecified"},
            files={"file": ("materials.csv", "ped_id,hyb_check,set\nA001,0,A\n", "text/csv")},
        )
        self.assertEqual(upload.status_code, 201, upload.text)
        response = await self.submit_message(
            conversation_id="conv-v2-schema-switch-unspecified",
            content="做间比法试验设计，田块10列",
            capability_id="skill.field_design",
            metadata={"upload_ids": [upload.json()["upload_id"]]},
        )
        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]
        await self.wait_for_condition(lambda: self.runtime.list_interrupts(task_id))
        interrupt = (await self.runtime.list_interrupts(task_id))[0]
        collection_id = interrupt["required_fields"][SLOT_COLLECTION_REF_FIELD]["collection_id"]
        before = await self.runtime.storage.get_slot_collection(collection_id)
        self.assertIsNotNone(before)
        assert before is not None
        self.assertEqual(before.selected_schema_id, "interval")
        self.assertIn("ck_spec", before.missing)

        answer = await self.client.post(
            "/api/v1/conversations/chat-messages",
            json={
                "conversation_id": "conv-v2-schema-switch-unspecified",
                "content": "改成对角线增广",
                "routing_mode": "auto",
                "capability_id": None,
                "client_message_id": "client-switch-unspecified",
                "metadata": {"interrupt_id": interrupt["interrupt_id"]},
            },
        )
        self.assertEqual(answer.status_code, 202, answer.text)
        payload = answer.json()
        self.assertEqual(payload["action"], "interrupt_schema_switched")
        self.assertTrue(payload["answer_payload"]["requires_confirmation"])
        self.assertIn("复用", payload["assistant_message"])
        after = await self.runtime.storage.get_slot_collection(collection_id)
        self.assertIsNotNone(after)
        assert after is not None
        self.assertEqual(after.selected_schema_id, "diagonal")
        self.assertEqual(after.status, "waiting_for_user")
        self.assertEqual(after.resolved["design"]["value"], "diagonal")
        self.assertNotIn("ncols", after.resolved)
        self.assertNotIn("material_data", after.resolved)
        events = await self.runtime.storage.list_slot_events(collection_id)
        self.assertIn("slot.schema_switched", [event.event_type for event in events])
        self.assertNotIn("slot.script_scheduled", [event.event_type for event in events])

        reuse_answer = await self.client.post(
            "/api/v1/conversations/chat-messages",
            json={
                "conversation_id": "conv-v2-schema-switch-unspecified",
                "content": "复用",
                "routing_mode": "auto",
                "capability_id": None,
                "client_message_id": "client-switch-unspecified-reuse",
                "metadata": {"interrupt_id": interrupt["interrupt_id"]},
            },
        )
        self.assertEqual(reuse_answer.status_code, 202, reuse_answer.text)
        reuse_payload = reuse_answer.json()
        self.assertEqual(reuse_payload["action"], "interrupt_schema_switched")
        self.assertTrue(reuse_payload["answer_payload"]["requires_confirmation"])
        reuse_switch = reuse_payload["answer_payload"]["schema_switch"]
        self.assertEqual(reuse_switch["reuse_decision"], "reuse")
        self.assertIn("ncols", reuse_switch["copied_fields"])
        self.assertIn("material_data", reuse_switch["copied_fields"])
        reused = await self.runtime.storage.get_slot_collection(collection_id)
        self.assertIsNotNone(reused)
        assert reused is not None
        self.assertEqual(reused.selected_schema_id, "diagonal")
        self.assertEqual(reused.status, "waiting_for_user")
        self.assertEqual(reused.missing, ())
        self.assertEqual(reused.resolved["ncols"]["value"], 10)
        self.assertEqual(reused.resolved["material_data"]["value"]["available"], True)
        self.assertEqual(reused.resolved["material_data"]["value"]["count"], 1)
        self.assertIn("确认执行", reused.last_question or "")
        events = await self.runtime.storage.list_slot_events(collection_id)
        event_types = [event.event_type for event in events]
        self.assertIn("slot.schema_switch_reuse_confirmed", event_types)
        self.assertIn("slot.confirmation_required", event_types)
        self.assertNotIn("slot.script_scheduled", event_types)

    async def test_interrupt_open_schema_switch_unspecified_reject_reuse_keeps_new_collection_empty(self) -> None:
        root = self.workspace / "skill-v2-schema-switch-no-reuse"
        self._write_field_design_upload_skill(root)

        async def slot_generator(prompt: str, **_kwargs) -> str:
            if "v2 Skill 参数补槽器" in prompt:
                return json.dumps(
                    {"resolved": {"ncols": {"raw_value": "田块10列", "value": 10, "source": "query"}}, "missing": []},
                    ensure_ascii=False,
                )
            if '"mode": "interrupt_turn_understanding"' in prompt:
                return json.dumps(
                    {
                        "parts": [
                            {
                                "part_id": "switch",
                                "kind": "schema_switch",
                                "text": "改成对角线增广",
                                "target_schema_id": "diagonal",
                                "reuse_decision": "unspecified",
                                "confidence": 0.98,
                            }
                        ],
                        "confidence": 0.98,
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
            data={"conversation_id": "conv-v2-schema-switch-no-reuse"},
            files={"file": ("materials.csv", "ped_id,hyb_check,set\nA001,0,A\n", "text/csv")},
        )
        self.assertEqual(upload.status_code, 201, upload.text)
        response = await self.submit_message(
            conversation_id="conv-v2-schema-switch-no-reuse",
            content="做间比法试验设计，田块10列",
            capability_id="skill.field_design",
            metadata={"upload_ids": [upload.json()["upload_id"]]},
        )
        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]
        await self.wait_for_condition(lambda: self.runtime.list_interrupts(task_id))
        interrupt = (await self.runtime.list_interrupts(task_id))[0]
        collection_id = interrupt["required_fields"][SLOT_COLLECTION_REF_FIELD]["collection_id"]

        switch = await self.client.post(
            "/api/v1/conversations/chat-messages",
            json={
                "conversation_id": "conv-v2-schema-switch-no-reuse",
                "content": "改成对角线增广",
                "routing_mode": "auto",
                "capability_id": None,
                "client_message_id": "client-switch-no-reuse",
                "metadata": {"interrupt_id": interrupt["interrupt_id"]},
            },
        )
        self.assertEqual(switch.status_code, 202, switch.text)

        reject_reuse = await self.client.post(
            "/api/v1/conversations/chat-messages",
            json={
                "conversation_id": "conv-v2-schema-switch-no-reuse",
                "content": "不复用",
                "routing_mode": "auto",
                "capability_id": None,
                "client_message_id": "client-switch-no-reuse-confirm",
                "metadata": {"interrupt_id": interrupt["interrupt_id"]},
            },
        )
        self.assertEqual(reject_reuse.status_code, 202, reject_reuse.text)
        payload = reject_reuse.json()
        self.assertEqual(payload["action"], "interrupt_schema_switched")
        self.assertFalse(payload["answer_payload"]["will_resume"])
        schema_switch = payload["answer_payload"]["schema_switch"]
        self.assertEqual(schema_switch["reuse_decision"], "do_not_reuse")
        self.assertEqual(schema_switch["copied_fields"], [])
        collection = await self.runtime.storage.get_slot_collection(collection_id)
        self.assertIsNotNone(collection)
        assert collection is not None
        self.assertEqual(collection.selected_schema_id, "diagonal")
        self.assertEqual(collection.resolved["design"]["value"], "diagonal")
        self.assertNotIn("ncols", collection.resolved)
        self.assertNotIn("material_data", collection.resolved)
        self.assertIn("ncols", collection.missing)
        self.assertIn("material_data", collection.missing)
        events = await self.runtime.storage.list_slot_events(collection_id)
        event_types = [event.event_type for event in events]
        self.assertIn("slot.schema_switch_reuse_confirmed", event_types)
        self.assertNotIn("slot.script_scheduled", event_types)

    async def test_interrupt_open_schema_switch_reuse_copies_same_fields_and_waits_for_execution_confirmation(self) -> None:
        root = self.workspace / "skill-v2-schema-switch-reuse"
        self._write_field_design_upload_skill(root)

        async def slot_generator(prompt: str, **_kwargs) -> str:
            if "v2 Skill 参数补槽器" in prompt:
                return json.dumps(
                    {"resolved": {"ncols": {"raw_value": "田块10列", "value": 10, "source": "query"}}, "missing": []},
                    ensure_ascii=False,
                )
            if '"mode": "interrupt_turn_understanding"' in prompt:
                return json.dumps(
                    {
                        "parts": [
                            {
                                "part_id": "switch",
                                "kind": "schema_switch",
                                "text": "改成对角线增广并复用已有参数",
                                "target_schema_id": "diagonal",
                                "reuse_decision": "reuse",
                                "execution_confirmation": False,
                                "execution_confirmation_confidence": 0.2,
                                "confidence": 0.98,
                            }
                        ],
                        "confidence": 0.98,
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
            data={"conversation_id": "conv-v2-schema-switch-reuse"},
            files={"file": ("materials.csv", "ped_id,hyb_check,set\nA001,0,A\n", "text/csv")},
        )
        self.assertEqual(upload.status_code, 201, upload.text)
        response = await self.submit_message(
            conversation_id="conv-v2-schema-switch-reuse",
            content="做间比法试验设计，田块10列",
            capability_id="skill.field_design",
            metadata={"upload_ids": [upload.json()["upload_id"]]},
        )
        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]
        await self.wait_for_condition(lambda: self.runtime.list_interrupts(task_id))
        interrupt = (await self.runtime.list_interrupts(task_id))[0]
        collection_id = interrupt["required_fields"][SLOT_COLLECTION_REF_FIELD]["collection_id"]

        answer = await self.client.post(
            "/api/v1/conversations/chat-messages",
            json={
                "conversation_id": "conv-v2-schema-switch-reuse",
                "content": "改成对角线增广并复用已有参数",
                "routing_mode": "auto",
                "capability_id": None,
                "client_message_id": "client-switch-reuse",
                "metadata": {"interrupt_id": interrupt["interrupt_id"]},
            },
        )
        self.assertEqual(answer.status_code, 202, answer.text)
        payload = answer.json()
        self.assertEqual(payload["action"], "interrupt_schema_switched")
        schema_switch = payload["answer_payload"]["schema_switch"]
        self.assertIn("ncols", schema_switch["copied_fields"])
        self.assertIn("ck_spec", schema_switch["discarded_fields"])
        self.assertTrue(payload["answer_payload"]["requires_confirmation"])
        after = await self.runtime.storage.get_slot_collection(collection_id)
        self.assertIsNotNone(after)
        assert after is not None
        self.assertEqual(after.selected_schema_id, "diagonal")
        self.assertEqual(after.status, "waiting_for_user")
        self.assertEqual(after.resolved["ncols"]["value"], 10)
        events = await self.runtime.storage.list_slot_events(collection_id)
        self.assertNotIn("slot.script_scheduled", [event.event_type for event in events])

    async def test_schema_switch_same_turn_reuses_upload_applies_new_ncols_and_resumes_when_confirmed(self) -> None:
        root = self.workspace / "skill-v2-schema-switch-same-turn-resume"
        self._write_field_design_upload_skill(root)

        async def slot_generator(prompt: str, **_kwargs) -> str:
            if "v2 Skill 参数补槽器" in prompt:
                return json.dumps(
                    {"resolved": {"ncols": {"raw_value": "田块10列", "value": 10, "source": "query"}}, "missing": []},
                    ensure_ascii=False,
                )
            if '"mode": "interrupt_turn_understanding"' in prompt:
                return json.dumps(
                    {
                        "parts": [
                            {
                                "part_id": "switch",
                                "kind": "schema_switch",
                                "text": "继续使用已上传材料，改成增广对角线设计，田块列数设为6，并立即执行",
                                "target_schema_id": "diagonal",
                                "reuse_decision": "reuse",
                                "execution_confirmation": False,
                                "execution_confirmation_confidence": 0.0,
                                "confidence": 0.99,
                            }
                        ],
                        "confidence": 0.99,
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
            data={"conversation_id": "conv-v2-schema-switch-same-turn-resume"},
            files={"file": ("materials.csv", "ped_id,hyb_check,set\nA001,0,A\n", "text/csv")},
        )
        self.assertEqual(upload.status_code, 201, upload.text)
        response = await self.submit_message(
            conversation_id="conv-v2-schema-switch-same-turn-resume",
            content="做间比法试验设计，田块10列",
            capability_id="skill.field_design",
            metadata={"upload_ids": [upload.json()["upload_id"]]},
        )
        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]
        await self.wait_for_condition(lambda: self.runtime.list_interrupts(task_id))
        interrupt = (await self.runtime.list_interrupts(task_id))[0]
        collection_id = interrupt["required_fields"][SLOT_COLLECTION_REF_FIELD]["collection_id"]

        answer = await self.client.post(
            "/api/v1/conversations/chat-messages",
            json={
                "conversation_id": "conv-v2-schema-switch-same-turn-resume",
                "content": "继续使用已上传材料，改成增广对角线设计，田块列数设为6，并立即执行",
                "routing_mode": "auto",
                "capability_id": None,
                "client_message_id": "client-switch-same-turn-resume",
                "metadata": {"interrupt_id": interrupt["interrupt_id"]},
            },
        )
        self.assertEqual(answer.status_code, 202, answer.text)
        payload = answer.json()
        self.assertEqual(payload["action"], "interrupt_resumed")
        self.assertTrue(payload["answer_payload"]["will_resume"])
        self.assertFalse(payload["answer_payload"]["requires_confirmation"])

        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")
        collection = await self.runtime.storage.get_slot_collection(collection_id)
        self.assertIsNotNone(collection)
        assert collection is not None
        self.assertEqual(collection.selected_schema_id, "diagonal")
        self.assertEqual(collection.resolved["ncols"]["value"], 6)
        self.assertIn(collection.status, {"script_scheduled", "completed"})
        events = await self.runtime.storage.list_slot_events(collection_id)
        self.assertIn("slot.script_scheduled", [event.event_type for event in events])

    async def test_removed_interrupt_answer_route_rejects_legacy_v2_payloads(self) -> None:
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

        removed_route = await self.client.post(
            "/api/v1/tasks/interrupts/answer",
            json={
                "task_id": task_id,
                "interrupt_id": interrupt["interrupt_id"],
                "client_request_id": "req-v2-removed-route",
                "answer_payload": {"design": "对角线增广"},
            },
        )
        self.assertEqual(removed_route.status_code, 404, removed_route.text)

    async def test_answered_v2_interrupt_replays_same_turn_but_rejects_new_key(self) -> None:
        root = self.workspace / "skill-v2-stale-interrupt"
        self._write_diagonal_skill(root)

        async def slot_generator(prompt: str, **_kwargs) -> str:
            if '"mode": "interrupt_turn_understanding"' in prompt:
                return json.dumps(
                    {
                        "parts": [{"part_id": "s1", "kind": "slot_answer", "text": "12列", "confidence": 0.98}],
                        "confidence": 0.98,
                    },
                    ensure_ascii=False,
                )
            if '"mode": "interrupt_resume_verification"' in prompt:
                return json.dumps({"allow_resume": True, "confidence": 0.99, "reason": "clear slot answer"}, ensure_ascii=False)
            if '"mode": "normal_extraction"' in prompt:
                return json.dumps(
                    {"resolved": {"ncols": {"raw_value": "12列", "value": 12, "source": "current_answer"}}},
                    ensure_ascii=False,
                )
            return "{}"

        await self.reconfigure_runtime(
            skill_roots=(root,),
            public_skill_roots=(root,),
            skill_input_text_generator=slot_generator,
            enable_skill_input_llm=True,
        )
        response = await self.submit_message(
            conversation_id="conv-v2-stale-interrupt",
            content="做对角线增广设计",
            capability_id="skill.field_design",
        )
        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]
        await self.wait_for_condition(lambda: self.runtime.list_interrupts(task_id))
        interrupt = (await self.runtime.list_interrupts(task_id))[0]
        collection_id = interrupt["required_fields"][SLOT_COLLECTION_REF_FIELD]["collection_id"]

        first = await self.client.post(
            "/api/v1/conversations/chat-messages",
            json={
                "conversation_id": "conv-v2-stale-interrupt",
                "content": "12列",
                "routing_mode": "auto",
                "capability_id": None,
                "client_message_id": "client-v2-stale-first",
                "metadata": {"interrupt_id": interrupt["interrupt_id"]},
            },
        )
        self.assertEqual(first.status_code, 202, first.text)
        self.assertEqual(first.json()["action"], "interrupt_resumed")
        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")
        persisted = await self.runtime.storage.get_interrupt(interrupt["interrupt_id"])
        self.assertIsNotNone(persisted)
        assert persisted is not None
        self.assertEqual(persisted.status, InterruptStatus.ANSWERED)

        messages_before = await self.runtime.storage.list_messages_for_conversation("conv-v2-stale-interrupt")
        slot_events_before = await self.runtime.storage.list_slot_events(collection_id)
        retry = await self.client.post(
            "/api/v1/conversations/chat-messages",
            json={
                "conversation_id": "conv-v2-stale-interrupt",
                "content": "12列",
                "routing_mode": "auto",
                "capability_id": None,
                "client_message_id": "client-v2-stale-first",
                "metadata": {"interrupt_id": interrupt["interrupt_id"]},
            },
        )
        self.assertEqual(retry.status_code, 202, retry.text)
        self.assertEqual(retry.json()["action"], "interrupt_resumed")
        self.assertEqual(
            len(await self.runtime.storage.list_messages_for_conversation("conv-v2-stale-interrupt")),
            len(messages_before),
        )
        self.assertEqual(len(await self.runtime.storage.list_slot_events(collection_id)), len(slot_events_before))

        stale_chat = await self.client.post(
            "/api/v1/conversations/chat-messages",
            json={
                "conversation_id": "conv-v2-stale-interrupt",
                "content": "13列",
                "routing_mode": "auto",
                "capability_id": None,
                "client_message_id": "client-v2-stale-new",
                "metadata": {"interrupt_id": interrupt["interrupt_id"]},
            },
        )
        self.assertEqual(stale_chat.status_code, 400, stale_chat.text)

        self.assertEqual(
            len(await self.runtime.storage.list_messages_for_conversation("conv-v2-stale-interrupt")),
            len(messages_before),
        )
        self.assertEqual(len(await self.runtime.storage.list_slot_events(collection_id)), len(slot_events_before))

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

        answer = await self.answer_interrupt_with_chat(
            conversation_id="conv-1",
            interrupt_id=interrupt["interrupt_id"],
            client_message_id="req-schema-select",
            content="间比法",
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
            if '"mode": "interrupt_turn_understanding"' in prompt:
                text = "还没有列数" if "还没有列数" in prompt else "历史里的列数"
                return json.dumps({"intent": "slot_answer", "confidence": 0.95, "reason": text}, ensure_ascii=False)
            if '"mode": "interrupt_resume_verification"' in prompt:
                return json.dumps({"allow_resume": True, "confidence": 0.96, "reason": "history recall answer"}, ensure_ascii=False)
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
        first = await self.answer_interrupt_with_chat(
            conversation_id="conv-v2-history-runtime",
            interrupt_id=first_interrupt["interrupt_id"],
            client_message_id="req-history-first",
            content="还没有列数",
        )
        self.assertEqual(first.status_code, 202, first.text)
        await self.runtime._await_existing_execution(task_id)
        second_interrupt = [
            interrupt for interrupt in await self.runtime.list_interrupts(task_id)
            if interrupt["status"] == "open"
        ][0]
        second = await self.answer_interrupt_with_chat(
            conversation_id="conv-v2-history-runtime",
            interrupt_id=second_interrupt["interrupt_id"],
            client_message_id="req-history-second",
            content="我之前不是告诉过你了吗",
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
        first = await self.answer_interrupt_with_chat(
            conversation_id="conv-v2-idempotent",
            interrupt_id=interrupt["interrupt_id"],
            client_message_id="req-v2-idempotent",
            content="12列",
        )
        self.assertEqual(first.status_code, 202, first.text)
        await self.wait_for_terminal_task(task_id)
        collection = await self.runtime.storage.get_slot_collection(collection_ref["collection_id"])
        self.assertIsNotNone(collection)
        assert collection is not None
        events_before = await self.runtime.storage.list_slot_events(collection.collection_id)
        messages_before = await self.runtime.storage.list_messages_for_conversation("conv-v2-idempotent")
        answers_before = await self.runtime.storage.list_interrupt_answers(interrupt["interrupt_id"])

        duplicate = await self.answer_interrupt_with_chat(
            conversation_id="conv-v2-idempotent",
            interrupt_id=interrupt["interrupt_id"],
            client_message_id="req-v2-idempotent",
            content="12列",
        )
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

        answer = await self.answer_interrupt_with_chat(
            conversation_id="conv-v2-old-embedded",
            interrupt_id=interrupt.interrupt_id,
            client_message_id="req-v2-old-embedded",
            content="12列",
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

    async def test_ready_collection_can_schedule_again_after_later_ready_revision(self) -> None:
        collection = SlotCollection(
            collection_id="slot-reschedule-ready",
            task_id="task-reschedule-ready",
            node_id="node-reschedule-ready",
            conversation_id="conv-reschedule-ready",
            capability_id="skill.field_design",
            skill_name="field-design",
            kind="input_collection",
            status="ready",
            revision=0,
            round=1,
            selected_schema_id="rcbd",
            selected_entrypoint="run",
        )
        await self.runtime.storage.save_slot_collection(collection)

        first, first_scheduled = await self.runtime._mark_v2_slot_script_scheduled(collection)
        self.assertTrue(first_scheduled)
        self.assertEqual(first.status, "script_scheduled")

        later_ready = replace(first, status="ready", revision=first.revision + 1, round=2)
        await self.runtime.storage.save_slot_collection(later_ready)

        second, second_scheduled = await self.runtime._mark_v2_slot_script_scheduled(later_ready)
        self.assertTrue(second_scheduled)
        self.assertEqual(second.status, "script_scheduled")
        events = await self.runtime.storage.list_slot_events(collection.collection_id)
        scheduled_events = [event for event in events if event.event_type == "slot.script_scheduled"]
        self.assertEqual(len(scheduled_events), 2)
        self.assertEqual(
            [event.idempotency_key for event in scheduled_events],
            [
                "slot:slot-reschedule-ready:script_scheduled:0",
                f"slot:slot-reschedule-ready:script_scheduled:{later_ready.revision}",
            ],
        )
