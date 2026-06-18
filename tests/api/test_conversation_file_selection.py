from __future__ import annotations

import json
import textwrap

from src.api.file_selection import FileRequirementProfile

from tests.api.support import APITestCase


class ConversationFileSelectionAPITest(APITestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.reconfigure_runtime(main_agent_stream_generator=lambda _prompt: ["完成。"])


    def _write_file_required_skill(self, root_name: str = "file-required-skill") -> str:
        skill_dir = self.workspace / root_name / "file-required"
        (skill_dir / "schemas").mkdir(parents=True)
        (skill_dir / "scripts").mkdir()
        (skill_dir / "SKILL.md").write_text(
            textwrap.dedent(
                """\
                ---
                name: file-required
                description: File required fixture.
                ---
                # File Required
                """
            ),
            encoding="utf-8",
        )
        (skill_dir / "skill.contract.yaml").write_text(
            textwrap.dedent(
                """\
                contract_version: '2'
                capability:
                  id: skill.file_required
                  display_name: File Required
                  description: Requires a file.
                routing:
                  triggers: [file required]
                file_intent:
                  requires_file: true
                  supported_file_types: [csv, txt]
                  description: 需要材料文件。
                runtime:
                  mode: python_subprocess
                entrypoints:
                  run:
                    path: scripts/run.py
                    input_schema: file_input
                    output: file_output
                input_schemas:
                  file_input:
                    path: schemas/file.input.yaml
                    title: File input
                outputs:
                  file_output:
                    required: [response_text]
                """
            ),
            encoding="utf-8",
        )
        (skill_dir / "schemas" / "file.input.yaml").write_text(
            textwrap.dedent(
                """\
                schema_id: file_input
                inputs:
                  material_file:
                    type: artifact
                    required: true
                    description: 材料文件
                    file_selection:
                      required: true
                      expected_content: [材料表]
                      supported_file_types: [csv]
                      helpful_columns: [ped_id]
                      disambiguation_hint: 优先选择材料表。
                """
            ),
            encoding="utf-8",
        )
        (skill_dir / "scripts" / "run.py").write_text("print('ok')\n", encoding="utf-8")
        return "skill.file_required"

    async def _upload_csv(self, conversation_id: str, filename: str, content: str = "ped_id,value\nA001,1\n") -> str:
        response = await self.client.post(
            "/api/v1/conversations/uploads",
            data={"conversation_id": conversation_id},
            files={"file": (filename, content, "text/csv")},
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["upload_id"]

    async def test_enforce_single_referenced_file_auto_binds_attachment(self) -> None:
        self.runtime._conversation_file_selector_mode = "enforce"
        conversation_id = "conv-file-auto"
        upload_id = await self._upload_csv(conversation_id, "materials.csv")

        response = await self.submit_message(
            conversation_id=conversation_id,
            capability_id=None,
            content="请用刚才上传的文件做一个摘要。",
        )

        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]
        await self.runtime._await_existing_execution(task_id)
        attachments = await self.runtime.storage.list_task_input_attachments_for_task(task_id)
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0].source_upload_id, upload_id)
        self.assertEqual(attachments[0].source_kind, "file_selector")
        self.assertNotIn("content", attachments[0].prompt_artifact)
        self.assertNotIn("content_base64", attachments[0].prompt_artifact)
        self.assertNotIn("storage_key", attachments[0].prompt_artifact)
        events = await self.runtime.storage.list_events_for_task(task_id)
        self.assertIn("conversation_file.file_selector_auto_bound", [event.event_type for event in events])

    async def test_shadow_records_audit_but_does_not_bind_or_interrupt(self) -> None:
        self.runtime._conversation_file_selector_mode = "shadow"
        conversation_id = "conv-file-shadow"
        await self._upload_csv(conversation_id, "materials.csv")

        response = await self.submit_message(
            conversation_id=conversation_id,
            capability_id=None,
            content="请用刚才上传的文件做摘要。",
        )

        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]
        await self.runtime._await_existing_execution(task_id)
        self.assertEqual(await self.runtime.storage.list_interrupts_for_task(task_id), [])
        self.assertEqual(await self.runtime.storage.list_task_input_attachments_for_task(task_id), [])
        event_types = [event.event_type for event in await self.runtime.storage.list_events_for_task(task_id)]
        self.assertIn("conversation_file.file_selector_invoked", event_types)
        self.assertIn("conversation_file.file_selector_decision_recorded", event_types)

    async def test_ambiguous_selection_interrupt_answer_by_upload_id_resumes(self) -> None:
        self.runtime._conversation_file_selector_mode = "enforce"
        conversation_id = "conv-file-ambiguous"
        first_upload_id = await self._upload_csv(conversation_id, "materials.csv", "ped_id,value\nA001,1\n")
        await self._upload_csv(conversation_id, "materials.csv", "ped_id,value\nB001,2\n")

        response = await self.submit_message(
            conversation_id=conversation_id,
            capability_id=None,
            content="请用上传的文件做摘要。",
        )

        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]
        interrupts = await self.runtime.list_interrupts(task_id)
        self.assertEqual(len(interrupts), 1)
        open_interrupt = interrupts[0]
        self.assertEqual(open_interrupt["reason_code"], "file_selection_ambiguous")
        self.assertEqual(open_interrupt["required_fields"]["_file_selection"]["presentation"], "natural_language")
        self.assertIn("replacement_file", open_interrupt["required_fields"])

        answer = await self.answer_interrupt_with_chat(
            conversation_id=conversation_id,
            interrupt_id=open_interrupt["interrupt_id"],
            content=f"用 {first_upload_id}",
        )

        self.assertEqual(answer.status_code, 202, answer.text)
        await self.runtime._await_existing_execution(task_id)
        attachments = await self.runtime.storage.list_task_input_attachments_for_task(task_id)
        self.assertEqual([attachment.source_upload_id for attachment in attachments], [first_upload_id])
        self.assertEqual((await self.runtime.list_interrupts(task_id))[0]["status"], "answered")
        saved_answers = await self.runtime.storage.list_interrupt_answers(open_interrupt["interrupt_id"])
        self.assertEqual(saved_answers[0].answer_payload["upload_ids"], [first_upload_id])

    async def test_file_requirement_profile_accepts_accepted_file_types_alias(self) -> None:
        profile = FileRequirementProfile.from_mapping({"required": True, "accepted_file_types": ["csv"]})

        self.assertEqual(profile.supported_file_types, ("csv",))

    async def test_required_file_with_no_active_files_opens_missing_file_clarification(self) -> None:
        self.runtime._conversation_file_selector_mode = "enforce"
        response = await self.submit_message(
            conversation_id="conv-file-required-missing",
            capability_id=None,
            content="请分析材料文件。",
            metadata={"file_requirement_profile": {"required": True, "expected_content": ["材料表"]}},
        )

        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]
        interrupts = await self.runtime.list_interrupts(task_id)
        self.assertEqual(len(interrupts), 1)
        self.assertEqual(interrupts[0]["reason_code"], "file_selection_ambiguous")
        self.assertEqual(interrupts[0]["required_fields"]["_file_selection"]["reason_code"], "no_files_in_conversation")
        self.assertIn("还没有可用文件", interrupts[0]["question"])

    async def test_selector_prompt_excludes_text_sample_and_raw_preview_values(self) -> None:
        captured_prompts: list[str] = []

        def selector_generator(prompt: str, **_kwargs) -> str:
            captured_prompts.append(prompt)
            return json.dumps({"decision": "ambiguous", "confidence": 0.4, "reason_code": "test"})

        await self.reconfigure_runtime(skill_input_text_generator=selector_generator)
        self.runtime._conversation_file_selector_mode = "enforce"
        conversation_id = "conv-file-redaction"
        await self._upload_csv(conversation_id, "secret.txt", "SECRET_VALUE_XYZ\nsecond line\n")
        await self._upload_csv(conversation_id, "materials.csv", "ped_id,value\nA001,1\n")

        response = await self.submit_message(
            conversation_id=conversation_id,
            capability_id=None,
            content="请用上传的文件做摘要。",
        )

        self.assertEqual(response.status_code, 202, response.text)
        self.assertTrue(captured_prompts)
        prompt = captured_prompts[0]
        self.assertNotIn("SECRET_VALUE_XYZ", prompt)
        self.assertNotIn("content_base64", prompt)
        self.assertNotIn("storage_key", prompt)
        self.assertNotIn("mount_path", prompt)

    async def test_required_file_skill_metadata_opens_selector_without_uploads(self) -> None:
        capability_id = self._write_file_required_skill()
        skill_root = self.workspace / "file-required-skill"
        await self.reconfigure_runtime(skill_roots=(skill_root,), public_skill_roots=(skill_root,))
        self.runtime._conversation_file_selector_mode = "enforce"

        response = await self.submit_message(
            conversation_id="conv-file-required-skill",
            capability_id=capability_id,
            content="执行这个 Skill。",
        )

        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]
        interrupts = await self.runtime.list_interrupts(task_id)
        self.assertEqual(len(interrupts), 1)
        required = interrupts[0]["required_fields"]["_file_selection"]
        self.assertEqual(required["reason_code"], "no_files_in_conversation")
        self.assertTrue(required["profile"]["required"])
        self.assertIn("材料表", required["profile"]["expected_content"])
