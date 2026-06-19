from __future__ import annotations

import json
import textwrap
from datetime import datetime, timedelta
from io import BytesIO

from openpyxl import Workbook

from src.api.file_selection import (
    FileRequirementProfile,
    FileRequirementProfileError,
    FileSelectionAnswerResolver,
    FileSelectionTriggerDetector,
    build_recent_usage,
    candidate_from_resource,
    deterministic_file_decision,
)
from src.core.models import TaskInputAttachment

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
                file_selection:
                  required: true
                  expected_content: [材料文件]
                  supported_file_types: [csv, txt]
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

    async def _upload_xlsx(
        self,
        conversation_id: str,
        filename: str,
        rows: list[list[object]] | None = None,
    ) -> str:
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "materials"
        for row in rows or [["材料名称", "是否对照", "组别"], ["A001", 0, 1]]:
            sheet.append(row)
        buffer = BytesIO()
        workbook.save(buffer)
        response = await self.client.post(
            "/api/v1/conversations/uploads",
            data={"conversation_id": conversation_id},
            files={
                "file": (
                    filename,
                    buffer.getvalue(),
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["upload_id"]

    async def test_active_conversation_file_is_available_without_task_attachment(self) -> None:
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
        self.assertEqual(attachments, [])
        resolved = await self.runtime.resolve_conversation_uploads_for_message(conversation_id, "acc-1")
        self.assertEqual([artifact["upload_id"] for artifact in resolved["uploaded_artifacts"]], [upload_id])
        events = await self.runtime.storage.list_events_for_task(task_id)
        self.assertNotIn("conversation_file.file_selector_auto_bound", [event.event_type for event in events])

    async def test_referenced_original_spreadsheet_filename_keeps_conversation_context_available(self) -> None:
        self.runtime._conversation_file_selector_mode = "enforce"
        conversation_id = "conv-file-original-name"
        await self._upload_xlsx(conversation_id, "间比法双组材料清单.xlsx")
        await self._upload_xlsx(conversation_id, "对角线增广列数20材料清单.xlsx")

        response = await self.submit_message(
            conversation_id=conversation_id,
            capability_id=None,
            content="那你用“间比法双组材料清单.xlsx”给我做个间比法试验设计。",
        )

        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]
        await self.runtime._await_existing_execution(task_id)
        attachments = await self.runtime.storage.list_task_input_attachments_for_task(task_id)
        self.assertEqual(attachments, [])
        resolved = await self.runtime.resolve_conversation_uploads_for_message(conversation_id, "acc-1")
        self.assertEqual([artifact["filename"] for artifact in resolved["uploaded_artifacts"]], ["间比法双组材料清单.xlsx", "对角线增广列数20材料清单.xlsx"])

    async def test_initial_message_ordinal_reference_keeps_conversation_context_available(self) -> None:
        self.runtime._conversation_file_selector_mode = "enforce"
        conversation_id = "conv-file-ordinal"
        await self._upload_xlsx(conversation_id, "对角线增广列数20材料清单.xlsx")
        await self._upload_xlsx(conversation_id, "间比法双组材料清单.xlsx")

        response = await self.submit_message(
            conversation_id=conversation_id,
            capability_id=None,
            content="那你用第一份文件帮我做一个对角线增广试验吧。",
        )

        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]
        await self.runtime._await_existing_execution(task_id)
        attachments = await self.runtime.storage.list_task_input_attachments_for_task(task_id)
        self.assertEqual(attachments, [])
        resolved = await self.runtime.resolve_conversation_uploads_for_message(conversation_id, "acc-1")
        self.assertEqual(len(resolved["uploaded_artifacts"]), 2)

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
        events = await self.runtime.storage.list_events_for_task(task_id)
        event_types = [event.event_type for event in events]
        self.assertIn("conversation_file.file_selector_invoked", event_types)
        self.assertIn("conversation_file.file_selector_decision_recorded", event_types)
        decision = next(event for event in events if event.event_type == "conversation_file.file_selector_decision_recorded")
        self.assertFalse(decision.payload["would_clarify"])
        self.assertTrue(decision.payload["would_auto_bind"])
        self.assertNotIn("user_message", json.dumps(decision.payload, ensure_ascii=False))
        self.assertNotIn("content_base64", json.dumps(decision.payload, ensure_ascii=False))
        self.assertNotIn("storage_key", json.dumps(decision.payload, ensure_ascii=False))

    async def test_multiple_active_files_are_available_without_selector_interrupt(self) -> None:
        self.runtime._conversation_file_selector_mode = "enforce"
        conversation_id = "conv-file-ambiguous"
        await self._upload_csv(conversation_id, "materials.csv", "ped_id,value\nA001,1\n")
        await self._upload_csv(conversation_id, "materials.csv", "ped_id,value\nB001,2\n")

        response = await self.submit_message(
            conversation_id=conversation_id,
            capability_id=None,
            content="请用上传的文件做摘要。",
        )

        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]
        interrupts = await self.runtime.list_interrupts(task_id)
        self.assertEqual(interrupts, [])
        await self.wait_for_terminal_task(task_id)
        attachments = await self.runtime.storage.list_task_input_attachments_for_task(task_id)
        self.assertEqual(attachments, [])
        resolved = await self.runtime.resolve_conversation_uploads_for_message(conversation_id, "acc-1")
        self.assertEqual(len(resolved["uploaded_artifacts"]), 2)

    async def test_file_requirement_profile_rejects_legacy_aliases(self) -> None:
        with self.assertRaisesRegex(FileRequirementProfileError, "accepted_file_types"):
            FileRequirementProfile.from_mapping({"required": True, "accepted_file_types": ["csv"]})

    async def test_request_metadata_allows_unrelated_intent_but_rejects_nested_legacy_fields(self) -> None:
        profile = self.runtime._file_requirement_profile_for_request(
            request=type("Request", (), {"metadata": {"intent": "qa"}, "content": "请解释概念"})(),
            metadata={"intent": "qa"},
        )
        self.assertFalse(profile.is_meaningful())

        with self.assertRaisesRegex(FileRequirementProfileError, "requires_file"):
            self.runtime._file_requirement_profile_for_request(
                request=type("Request", (), {"metadata": {"file_selection": {"requires_file": True}}, "content": "请处理文件"})(),
                metadata={"file_selection": {"requires_file": True}},
            )

    async def test_file_requirement_profile_accepts_final_fields(self) -> None:
        profile = FileRequirementProfile.from_mapping(
            {
                "source": "metadata",
                "required": True,
                "allow_multiple": False,
                "expected_content": ["材料表"],
                "supported_file_types": ["csv"],
                "helpful_columns": ["ped_id"],
                "disambiguation_hint": "优先材料表",
                "context_notes": ["metadata declared final file_selection"],
            }
        )

        self.assertTrue(profile.required)
        self.assertEqual(profile.supported_file_types, ("csv",))
        self.assertEqual(profile.source, "metadata")

    async def test_trigger_detector_ignores_ordinary_query_and_declines(self) -> None:
        detector = FileSelectionTriggerDetector()

        self.assertEqual(
            detector.should_trigger(
                text="请解释一下区组设计的基本概念。",
                profile=FileRequirementProfile(),
                has_explicit_uploads=False,
                active_file_count=2,
            ),
            (False, "ordinary_query"),
        )
        self.assertEqual(
            detector.should_trigger(
                text="这次不用上传的文件，只回答概念。",
                profile=FileRequirementProfile(required=True),
                has_explicit_uploads=False,
                active_file_count=2,
            ),
            (False, "explicit_or_declined"),
        )

    async def test_trigger_detector_continuation_requires_recent_usage_shadow(self) -> None:
        trigger, reason = FileSelectionTriggerDetector().should_trigger(
            text="继续用刚才那个数据。",
            profile=FileRequirementProfile(),
            has_explicit_uploads=False,
            active_file_count=2,
        )

        self.assertTrue(trigger)
        self.assertEqual(reason, "recent_usage_reference")

    async def test_upload_id_token_requires_exact_generated_shape(self) -> None:
        conversation_id = "conv-file-upload-id-token"
        await self._upload_csv(conversation_id, "materials.csv")
        resources = await self.runtime.storage.list_conversation_file_resources(
            conversation_id,
            "acc-1",
            include_deleted=False,
        )
        candidates = tuple(candidate_from_resource(resource) for resource in resources)

        embedded_existing = deterministic_file_decision(
            text=f"请使用 xxx{candidates[0].upload_id}yyy 这个文件。",
            profile=FileRequirementProfile(),
            candidates=candidates,
        )
        self.assertNotEqual(embedded_existing.reason_code, "explicit_upload_id")
        answer_embedded_existing = FileSelectionAnswerResolver().resolve(
            f"xxx{candidates[0].upload_id}yyy",
            candidates,
        )
        self.assertNotEqual(answer_embedded_existing.reason_code, "explicit_upload_id")

        embedded = deterministic_file_decision(
            text="请使用 xxxupl-abcdef123456yyy 这个文件。",
            profile=FileRequirementProfile(),
            candidates=candidates,
        )
        self.assertNotEqual(embedded.reason_code, "unknown_upload_id")

        exact_unknown = deterministic_file_decision(
            text="请使用 upl-abcdef123456 这个文件。",
            profile=FileRequirementProfile(),
            candidates=candidates,
        )
        self.assertEqual(exact_unknown.reason_code, "unknown_upload_id")

        trigger, reason = FileSelectionTriggerDetector().should_trigger(
            text="请使用 xxxupl-abcdef123456yyy 这个文件。",
            profile=FileRequirementProfile(),
            has_explicit_uploads=False,
            active_file_count=1,
        )
        self.assertTrue(trigger)
        self.assertEqual(reason, "query_reference")

        trigger, reason = FileSelectionTriggerDetector().should_trigger(
            text="请使用 upl-abcdef123456 这个文件。",
            profile=FileRequirementProfile(),
            has_explicit_uploads=False,
            active_file_count=1,
        )
        self.assertTrue(trigger)
        self.assertEqual(reason, "explicit_upload_id_reference")

    async def test_explicit_upload_ids_skip_shadow_selector(self) -> None:
        self.runtime._conversation_file_selector_mode = "shadow"
        conversation_id = "conv-file-explicit-skip"
        upload_id = await self._upload_csv(conversation_id, "materials.csv")

        response = await self.submit_message(
            conversation_id=conversation_id,
            capability_id=None,
            content="请用显式文件做摘要。",
            metadata={"upload_ids": [upload_id], "file_requirement_profile": {"required": True}},
        )

        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]
        await self.runtime._await_existing_execution(task_id)
        event_types = [event.event_type for event in await self.runtime.storage.list_events_for_task(task_id)]
        self.assertNotIn("conversation_file.file_selector_invoked", event_types)

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

        resources = await self.runtime.storage.list_conversation_file_resources(
            conversation_id,
            "acc-1",
            include_deleted=False,
        )
        candidates = tuple(candidate_from_resource(resource) for resource in resources)
        decision = await self.runtime._conversation_file_selection_decision(
            text="请用上传的文件做摘要。",
            profile=FileRequirementProfile(),
            candidates=candidates,
            metadata={},
        )

        self.assertEqual(decision.decision, "ambiguous")
        self.assertTrue(captured_prompts)
        prompt = captured_prompts[0]
        self.assertNotIn("SECRET_VALUE_XYZ", prompt)
        self.assertNotIn("content_base64", prompt)
        self.assertNotIn("storage_key", prompt)
        self.assertNotIn("mount_path", prompt)

    async def test_shadow_records_invalid_selector_output_without_raw_prompt(self) -> None:
        def selector_generator(_prompt: str, **_kwargs) -> str:
            return "not-json SECRET_VALUE_SHOULD_NOT_BE_AUDITED"

        await self.reconfigure_runtime(skill_input_text_generator=selector_generator)
        self.runtime._conversation_file_selector_mode = "shadow"
        conversation_id = "conv-file-invalid-selector"
        await self._upload_csv(conversation_id, "first.csv")
        await self._upload_csv(conversation_id, "second.csv")

        response = await self.submit_message(
            conversation_id=conversation_id,
            capability_id=None,
            content="请用上传的文件做摘要。",
        )

        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]
        await self.runtime._await_existing_execution(task_id)
        events = await self.runtime.storage.list_events_for_task(task_id)
        invalid = next(event for event in events if event.event_type == "conversation_file.file_selector_invalid_output")
        payload_text = json.dumps(invalid.payload, ensure_ascii=False)
        self.assertEqual(invalid.payload["reason_code"], "invalid_json")
        self.assertNotIn("SECRET_VALUE_SHOULD_NOT_BE_AUDITED", payload_text)
        self.assertNotIn("user_message", payload_text)
        self.assertNotIn("content_base64", payload_text)
        self.assertNotIn("storage_key", payload_text)

    async def test_shadow_audit_sanitizes_profile_free_text(self) -> None:
        self.runtime._conversation_file_selector_mode = "shadow"
        conversation_id = "conv-file-audit-profile-redaction"
        await self._upload_csv(conversation_id, "materials.csv")

        response = await self.submit_message(
            conversation_id=conversation_id,
            capability_id=None,
            content="请用上传的文件做摘要。",
            metadata={
                "file_requirement_profile": {
                    "required": True,
                    "disambiguation_hint": "storage_key=/tmp/private/secret.txt",
                    "context_notes": [
                        "token=abc123",
                        "api_key=xyz",
                        "Authorization: Bearer abc",
                        "private_key=hidden",
                        "safe note",
                    ],
                    "expected_content": ["content_base64=abc", "材料表"],
                }
            },
        )

        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]
        await self.runtime._await_existing_execution(task_id)
        decision = next(
            event
            for event in await self.runtime.storage.list_events_for_task(task_id)
            if event.event_type == "conversation_file.file_selector_decision_recorded"
        )
        payload_text = json.dumps(decision.payload, ensure_ascii=False)
        self.assertIn("safe note", payload_text)
        self.assertIn("材料表", payload_text)
        self.assertNotIn("storage_key", payload_text)
        self.assertNotIn("/tmp/private", payload_text)
        self.assertNotIn("token=abc123", payload_text)
        self.assertNotIn("api_key=xyz", payload_text)
        self.assertNotIn("Authorization", payload_text)
        self.assertNotIn("Bearer abc", payload_text)
        self.assertNotIn("private_key=hidden", payload_text)
        self.assertNotIn("content_base64=abc", payload_text)

    async def test_selector_candidates_exclude_deleted_resources(self) -> None:
        conversation_id = "conv-file-selector-deleted"
        active_id = await self._upload_csv(conversation_id, "active.csv")
        deleted_id = await self._upload_csv(conversation_id, "deleted.csv")
        deleted = await self.client.request(
            "DELETE",
            "/api/v1/conversations/uploads",
            json={"conversation_id": conversation_id, "upload_id": deleted_id},
        )
        self.assertEqual(deleted.status_code, 200, deleted.text)

        resources = await self.runtime.storage.list_conversation_file_resources(
            conversation_id,
            "acc-1",
            include_deleted=False,
        )
        candidates = tuple(candidate_from_resource(resource) for resource in resources)

        self.assertEqual([candidate.upload_id for candidate in candidates], [active_id])
        decision = deterministic_file_decision(
            text=f"请继续使用 {deleted_id} 这个文件。",
            profile=FileRequirementProfile(),
            candidates=candidates,
        )
        self.assertEqual(decision.decision, "ambiguous")
        self.assertEqual(decision.reason_code, "unknown_upload_id")

    async def test_recent_usage_ignores_file_upload_history_without_task_attachment(self) -> None:
        conversation_id = "conv-file-recent-history"
        first_id = await self._upload_csv(conversation_id, "first.csv")
        second_id = await self._upload_csv(conversation_id, "second.csv")
        resources = await self.runtime.storage.list_conversation_file_resources(
            conversation_id,
            "acc-1",
            include_deleted=False,
        )
        candidates = tuple(candidate_from_resource(resource, recent_usage=build_recent_usage(()).get(resource.file_id)) for resource in resources)

        self.assertEqual({candidate.upload_id for candidate in candidates}, {first_id, second_id})
        self.assertTrue(all(candidate.recent_usage is None for candidate in candidates))
        no_provenance = deterministic_file_decision(
            text="继续用刚才的文件。",
            profile=FileRequirementProfile(),
            candidates=candidates,
        )
        self.assertEqual(no_provenance.decision, "ambiguous")
        self.assertEqual(no_provenance.reason_code, "multiple_candidates")

        used_at = datetime(2026, 6, 19, 12, 0, 0)
        recent_usage = build_recent_usage(
            (
                TaskInputAttachment(
                    attachment_id="att-recent",
                    task_id="task-recent",
                    conversation_id=conversation_id,
                    source_kind="file_selector",
                    source_upload_id=second_id,
                    filename="second.csv",
                    created_at=used_at - timedelta(minutes=1),
                    updated_at=used_at,
                ),
            )
        )
        candidates_with_usage = tuple(candidate_from_resource(resource, recent_usage=recent_usage.get(resource.file_id)) for resource in resources)
        with_provenance = deterministic_file_decision(
            text="继续用刚才的文件。",
            profile=FileRequirementProfile(),
            candidates=candidates_with_usage,
        )

        self.assertEqual(with_provenance.decision, "select_one")
        self.assertEqual(with_provenance.upload_ids, (second_id,))
        self.assertEqual(with_provenance.reason_code, "recent_usage")

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
