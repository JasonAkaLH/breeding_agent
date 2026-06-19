from __future__ import annotations

import json
import textwrap

from src.api.dto import SubmitMessageRequest
from src.core.enums import RoutingMode
from src.core.models import PendingSkillContext
from src.integrations.agent_skills.missing_input_interrupt import SLOT_COLLECTION_REF_FIELD
from tests.api.support import APITestCase


class PendingSkillContextAPITest(APITestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        root = self.workspace / "pending-skills"
        self._write_need_variety_skill(root)
        self._write_material_ncols_skill(root)
        self._write_plain_fail_skill(root)
        await self.reconfigure_runtime(
            skill_roots=(self.default_project_skill_root, root),
            public_skill_roots=(self.default_project_skill_root, root),
            enable_skill_input_llm=False,
        )

    def _interrupt_missing_fields(self, interrupt: dict[str, object]) -> list[str]:
        required_fields = interrupt.get("required_fields")
        if isinstance(required_fields, dict):
            slot_ref = required_fields.get(SLOT_COLLECTION_REF_FIELD)
            if isinstance(slot_ref, dict):
                missing = slot_ref.get("missing")
                if isinstance(missing, list):
                    return [str(field) for field in missing]
            return [str(field) for field in required_fields if not str(field).startswith("_")]
        if isinstance(required_fields, list):
            return [str(field) for field in required_fields]
        return []

    def _write_need_variety_skill(self, root) -> None:
        skill_dir = root / "need-variety"
        scripts = skill_dir / "scripts"
        schemas = skill_dir / "schemas"
        scripts.mkdir(parents=True)
        schemas.mkdir(parents=True)
        (scripts / "answer.py").write_text(
            textwrap.dedent(
                """
                import json, sys
                payload = json.load(sys.stdin)
                print(json.dumps({"answer": "已查询 " + str(payload.get("variety"))}, ensure_ascii=False))
                """
            ).strip(),
            encoding="utf-8",
        )
        (skill_dir / "SKILL.md").write_text(
            """---
name: need-variety
description: 需要品种名的测试 Skill
---

# Need Variety
""",
            encoding="utf-8",
        )
        (skill_dir / "skill.contract.yaml").write_text(
            """contract_version: '2'
capability: {id: skill.need_variety, display_name: Need Variety}
runtime: {mode: python_subprocess, answer_mode: direct}
schema_selector: {strategy: deterministic_then_llm, selector_field: kind, default: lookup}
entrypoints: {run: {path: scripts/answer.py}}
input_schemas:
  lookup: {path: schemas/lookup.input.yaml, aliases: [查询, 品种], entrypoint: run}
""",
            encoding="utf-8",
        )
        (schemas / "lookup.input.yaml").write_text(
            """schema_id: lookup
inputs:
  kind: {type: string, required: true, const: lookup, aliases: [查询]}
  variety:
    type: string
    required: true
    aliases: [品种, 品种名称]
    patterns:
      - '品种(?:是|为)?([\\w\\u4e00-\\u9fff]+)'
""",
            encoding="utf-8",
        )

    def _write_material_ncols_skill(self, root) -> None:
        skill_dir = root / "material-ncols"
        scripts = skill_dir / "scripts"
        schemas = skill_dir / "schemas"
        scripts.mkdir(parents=True)
        schemas.mkdir(parents=True)
        (scripts / "answer.py").write_text(
            textwrap.dedent(
                """
                import json, sys

                payload = json.load(sys.stdin)
                if not payload.get("ncols"):
                    print(json.dumps({
                        "answer": "缺少田块列数",
                        "error": {"type": "missing_input"},
                        "missing": ["ncols"],
                        "message": "缺少田块列数",
                    }, ensure_ascii=False))
                    raise SystemExit(0)
                print(json.dumps({
                    "answer": "材料和列数都已收到",
                    "ncols": payload.get("ncols"),
                    "artifact_count": len(payload.get("uploaded_artifacts") or []),
                }, ensure_ascii=False))
                """
            ).strip(),
            encoding="utf-8",
        )
        (skill_dir / "SKILL.md").write_text(
            """---
name: material-ncols
description: 需要先上传材料、再补充列数的测试 Skill
---

# Material Ncols
""",
            encoding="utf-8",
        )
        (skill_dir / "skill.contract.yaml").write_text(
            """contract_version: '2'
capability: {id: skill.material_ncols, display_name: Material Ncols}
runtime: {mode: python_subprocess, answer_mode: direct}
schema_selector: {strategy: deterministic_then_llm, selector_field: design, default: diagonal}
entrypoints: {run: {path: scripts/answer.py}}
input_schemas:
  diagonal: {path: schemas/diagonal.input.yaml, aliases: [增广对角线, 对角线设计], entrypoint: run}
""",
            encoding="utf-8",
        )
        (schemas / "diagonal.input.yaml").write_text(
            """schema_id: diagonal
inputs:
  design: {type: string, required: true, const: diagonal, aliases: [增广对角线, 对角线设计]}
  material_data:
    type: artifact
    required: true
    source: {allowed: [artifact]}
    aliases: [材料清单, material_data]
    file_selection:
      required: true
      allow_multiple: false
      expected_content: [材料清单]
      supported_file_types: [csv]
      helpful_columns: [ped_id]
      disambiguation_hint: 优先选择本会话最近用于设计任务的材料清单。
  ncols:
    type: integer
    required: true
    aliases: [ncols, 田块列数]
    patterns:
      - 'ncols\\s*[=：:]\\s*(\\d+)'
      - '(\\d+)\\s*个田块'
""",
            encoding="utf-8",
        )

    def _write_plain_fail_skill(self, root) -> None:
        skill_dir = root / "plain-fail"
        scripts = skill_dir / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "fail.py").write_text(
            "import sys\nprint('plain failure', file=sys.stderr)\nsys.exit(2)\n",
            encoding="utf-8",
        )
        (skill_dir / "SKILL.md").write_text(
            """---
name: plain-fail
description: 返回普通失败的测试 Skill
---

# Plain Fail
""",
            encoding="utf-8",
        )
        (skill_dir / "skill.contract.yaml").write_text(
            """contract_version: '2'
capability: {id: skill.plain_fail, display_name: Plain Fail}
runtime: {mode: python_subprocess, answer_mode: direct}
entrypoints: {run: {path: scripts/fail.py}}
""",
            encoding="utf-8",
        )

    async def _force_need_variety(self, content: str, conversation_id: str = "conv-pending"):
        return await self.client.post(
            "/api/v1/conversations/chat-messages",
            json={
                "conversation_id": conversation_id,
                "content": content,
                "routing_mode": "force_capability",
                "capability_id": "main_agent.respond",
                "metadata": {
                    "forced_by_slash_command": True,
                    "slash_command": "/need-variety",
                    "soft_skill_binding": {"capability_id": "skill.need_variety", "command": "/need-variety"},
                },
            },
        )

    async def _wait_for_open_interrupt(self, task_id: str) -> dict[str, object]:
        await self.runtime._await_existing_execution(task_id)

        async def has_open_interrupt() -> bool:
            interrupts = await self.runtime.storage.list_interrupts_for_task(task_id)
            return any(str(interrupt.status) == "open" for interrupt in interrupts)

        await self.wait_for_condition(has_open_interrupt)
        interrupts = await self.runtime.list_interrupts(task_id)
        return next(interrupt for interrupt in interrupts if interrupt["status"] == "open")

    async def test_force_skill_missing_input_creates_open_interrupt_for_frontend_card(self) -> None:
        first = await self._force_need_variety("请查询")
        self.assertEqual(first.status_code, 202)
        first_task_id = first.json()["task_id"]
        interrupt = await self._wait_for_open_interrupt(first_task_id)

        self.assertEqual(interrupt["reason_code"], "missing_variety")
        self.assertIn("variety", interrupt["question"])
        self.assertIn("variety", self._interrupt_missing_fields(interrupt))
        self.assertIsNone(await self.runtime.storage.get_active_pending_skill_context("conv-pending"))
        events = await self.runtime.storage.list_events_for_task(first_task_id)
        self.assertIn("skill.input_missing", [event.event_type for event in events])
        self.assertNotIn("pending_skill_context.created", [event.event_type for event in events])

        conversation = await self.runtime.storage.get_conversation("conv-pending")
        self.assertEqual(conversation.current_task_id, first_task_id)

    async def test_interrupt_answer_resumes_skill_and_consumes_no_pending_context(self) -> None:
        first = await self._force_need_variety("请查询")
        self.assertEqual(first.status_code, 202)
        task_id = first.json()["task_id"]
        interrupt = await self._wait_for_open_interrupt(task_id)
        interrupted_node_id = str(interrupt["node_id"])
        original_task = await self.runtime.storage.get_task(task_id)
        self.assertIsNotNone(original_task)
        assert original_task is not None
        original_root_node_id = original_task.root_node_id
        nodes_before_answer = {node.node_id: node for node in await self.runtime.storage.list_task_nodes_for_task(task_id)}
        edges_before_answer = await self.runtime.storage.list_task_edges(task_id)
        finalizer_node_ids = [
            edge.to_node_id
            for edge in edges_before_answer
            if edge.from_node_id == interrupted_node_id and nodes_before_answer[edge.to_node_id].capability_id == "main_agent.respond"
        ]
        self.assertLessEqual(len(finalizer_node_ids), 1)
        original_finalizer_node_id = finalizer_node_ids[0] if finalizer_node_ids else None

        answer = await self.answer_interrupt_with_chat(
            conversation_id="conv-pending",
            interrupt_id=interrupt["interrupt_id"],
            content="品种是龙粳33",
            metadata={
                "macro_expansion": True,
                "macro_input_payload": {"subtask_label": "malicious"},
                "resume_interrupted_node_id": f"{task_id}:forged-skill",
                "resume_finalizer_node_id": f"{task_id}:forged-finalizer",
                "skill_bundle_revision": "skillrev-forged",
            },
        )
        self.assertEqual(answer.status_code, 202)
        await self.runtime._await_existing_execution(task_id)
        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")
        self.assertEqual(terminal["active_node_count"], 0)
        self.assertEqual(terminal["root_node_id"], original_root_node_id)

        nodes = await self.runtime.storage.list_task_nodes_for_task(task_id)
        skill_nodes = [node for node in nodes if node.capability_id == "skill.need_variety"]
        self.assertEqual([node.node_id for node in skill_nodes], [interrupted_node_id])
        self.assertTrue(all(str(node.status) == "completed" for node in skill_nodes))
        self.assertFalse(
            any(node.node_id == f"{task_id}:skill_execute" for node in nodes if node.node_id != interrupted_node_id),
            "interrupt resume must reuse the interrupted dynamic Skill node instead of creating a second direct Skill node",
        )
        if original_finalizer_node_id is not None:
            finalizer_node = next(node for node in nodes if node.node_id == original_finalizer_node_id)
            self.assertEqual(str(finalizer_node.status), "completed")

        task = await self.runtime.storage.get_task(task_id)
        self.assertEqual(task.routing_mode, RoutingMode.FORCE_CAPABILITY)
        self.assertEqual(task.requested_capability_id, "skill.need_variety")
        self.assertIsNone(await self.runtime.storage.get_active_pending_skill_context("conv-pending"))
        events = await self.runtime.storage.list_events_for_task(task_id)
        self.assertIn("task.interrupt_answered", [event.event_type for event in events])
        self.assertIn("skill.execution_completed", [event.event_type for event in events])
        self.assertNotIn("pending_skill_context.consumed", [event.event_type for event in events])

    async def test_interrupt_resume_reuses_previous_upload_answer_across_multiple_missing_inputs(self) -> None:
        response = await self.submit_message(
            conversation_id="conv-multi-interrupt",
            content="请做一个增广对角线设计方案",
            capability_id="skill.material_ncols",
        )
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]

        material_interrupt = await self._wait_for_open_interrupt(task_id)
        self.assertIn("material_data", self._interrupt_missing_fields(material_interrupt))

        upload = await self.client.post(
            "/api/v1/conversations/uploads",
            data={"conversation_id": "conv-multi-interrupt"},
            files={"file": ("materials.csv", "ped_id,hyb_check,set\nCK,CK,A\nA001,Test,A\n", "text/csv")},
        )
        self.assertEqual(upload.status_code, 201)
        upload_id = upload.json()["upload_id"]

        first_answer = await self.answer_interrupt_with_chat(
            conversation_id="conv-multi-interrupt",
            interrupt_id=material_interrupt["interrupt_id"],
            content="我上传了材料文件",
            metadata={"upload_ids": [upload_id]},
        )
        self.assertEqual(first_answer.status_code, 202)

        ncols_interrupt = await self._wait_for_open_interrupt(task_id)
        self.assertIn("ncols", self._interrupt_missing_fields(ncols_interrupt))

        second_answer = await self.answer_interrupt_with_chat(
            conversation_id="conv-multi-interrupt",
            interrupt_id=ncols_interrupt["interrupt_id"],
            content="20个田块",
        )
        self.assertEqual(second_answer.status_code, 202)
        await self.runtime._await_existing_execution(task_id)

        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")
        self.assertEqual(terminal["active_node_count"], 0)

        nodes = await self.runtime.storage.list_task_nodes_for_task(task_id)
        skill_nodes = [node for node in nodes if node.capability_id == "skill.material_ncols"]
        self.assertEqual(len(skill_nodes), 1)
        self.assertEqual(str(skill_nodes[0].status), "completed")

        events = await self.runtime.storage.list_events_for_task(task_id)
        resolved_events = [event for event in events if event.event_type == "skill.input_resolved"]
        self.assertTrue(
            any(
                "material_data" in event.payload.get("resolved_fields", ())
                and "ncols" in event.payload.get("resolved_fields", ())
                for event in resolved_events
            )
        )

    async def test_initial_upload_is_reused_when_resume_answers_only_scalar_missing_input(self) -> None:
        upload = await self.client.post(
            "/api/v1/conversations/uploads",
            data={"conversation_id": "conv-initial-upload"},
            files={"file": ("materials.csv", "ped_id,hyb_check,set\nCK,CK,A\nA001,Test,A\n", "text/csv")},
        )
        self.assertEqual(upload.status_code, 201, upload.text)
        upload_id = upload.json()["upload_id"]

        response = await self.submit_message(
            conversation_id="conv-initial-upload",
            content="请做一个增广对角线设计方案",
            capability_id="skill.material_ncols",
            metadata={"upload_ids": [upload_id]},
        )
        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]

        ncols_interrupt = await self._wait_for_open_interrupt(task_id)
        self.assertEqual(ncols_interrupt["reason_code"], "missing_ncols")
        self.assertIn("ncols", self._interrupt_missing_fields(ncols_interrupt))

        answer = await self.answer_interrupt_with_chat(
            conversation_id="conv-initial-upload",
            interrupt_id=ncols_interrupt["interrupt_id"],
            content="20个田块",
        )
        self.assertEqual(answer.status_code, 202, answer.text)
        await self.runtime._await_existing_execution(task_id)

        interrupts = await self.runtime.list_interrupts(task_id)
        self.assertFalse(
            any(item["status"] == "open" and item["reason_code"] == "missing_material_data" for item in interrupts),
            f"initial task upload must remain bound after scalar resume; interrupts={interrupts!r}",
        )
        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")

        events = await self.runtime.storage.list_events_for_task(task_id)
        resolved_events = [event for event in events if event.event_type == "skill.input_resolved"]
        self.assertTrue(
            any(
                "material_data" in event.payload.get("resolved_fields", ())
                and "ncols" in event.payload.get("resolved_fields", ())
                for event in resolved_events
            )
        )
        attachments = await self.runtime.storage.list_task_input_attachments_for_task(task_id)
        self.assertEqual(len(attachments), 1)
        self.assertNotIn("content", attachments[0].prompt_artifact)
        self.assertNotIn("content_base64", attachments[0].prompt_artifact)
        self.assertIn("content", attachments[0].skill_artifact)
        event_payloads = json.dumps([event.payload for event in events], ensure_ascii=False, default=str)
        self.assertNotIn("ped_id,hyb_check,set", event_payloads)

    async def test_explicit_upload_ids_bind_task_attachment_and_preserve_source_kind(self) -> None:
        upload = await self.client.post(
            "/api/v1/conversations/uploads",
            data={"conversation_id": "conv-explicit-upload"},
            files={"file": ("materials.csv", "ped_id,hyb_check,set\nCK,CK,A\nA001,Test,A\n", "text/csv")},
        )
        self.assertEqual(upload.status_code, 201, upload.text)
        upload_id = upload.json()["upload_id"]

        response = await self.submit_message(
            conversation_id="conv-explicit-upload",
            content="请使用这个材料文件做设计",
            capability_id="main_agent.respond",
            metadata={"upload_ids": [upload_id]},
        )

        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]
        attachments = await self.runtime.storage.list_task_input_attachments_for_task(task_id)
        self.assertEqual(len(attachments), 1)
        attachment = attachments[0]
        self.assertEqual(attachment.source_kind, "message_upload")
        self.assertEqual(attachment.source_upload_id, upload_id)
        self.assertEqual(attachment.source_message_id, response.json()["message_id"])
        self.assertNotIn("content", attachment.prompt_artifact)
        self.assertNotIn("content_base64", attachment.prompt_artifact)
        self.assertNotIn("storage_key", attachment.prompt_artifact)
        self.assertIn("content", attachment.skill_artifact)

    async def test_missing_upload_id_in_message_metadata_fails_before_message_or_task(self) -> None:
        response = await self.submit_message(
            conversation_id="conv-missing-upload-id",
            content="请使用缺失文件",
            capability_id="main_agent.respond",
            metadata={"upload_ids": ["upl-missing"]},
        )

        self.assertEqual(response.status_code, 400, response.text)
        self.assertIsNone(await self.runtime.storage.get_conversation("conv-missing-upload-id"))
        self.assertEqual(await self.runtime.storage.list_messages_for_conversation("conv-missing-upload-id"), [])
        self.assertEqual(await self.runtime.storage.list_tasks_for_conversation("conv-missing-upload-id"), [])
        self.assertEqual(
            await self.runtime.storage.list_task_input_attachments_for_conversation("conv-missing-upload-id"),
            [],
        )

    async def test_deleted_upload_id_in_message_metadata_fails_before_message_or_task(self) -> None:
        upload = await self.client.post(
            "/api/v1/conversations/uploads",
            data={"conversation_id": "conv-deleted-upload-id"},
            files={"file": ("materials.csv", "ped_id,hyb_check,set\nCK,CK,A\nA001,Test,A\n", "text/csv")},
        )
        self.assertEqual(upload.status_code, 201, upload.text)
        upload_id = upload.json()["upload_id"]
        deleted = await self.client.request(
            "DELETE",
            "/api/v1/conversations/uploads",
            json={"conversation_id": "conv-deleted-upload-id", "upload_id": upload_id},
        )
        self.assertEqual(deleted.status_code, 200, deleted.text)
        before = await self.runtime.storage.get_conversation("conv-deleted-upload-id")
        before_message_ids = [
            message.message_id
            for message in await self.runtime.storage.list_messages_for_conversation("conv-deleted-upload-id")
        ]

        response = await self.submit_message(
            conversation_id="conv-deleted-upload-id",
            content="继续使用刚才删除的文件",
            capability_id="main_agent.respond",
            metadata={"upload_ids": [upload_id]},
        )

        self.assertEqual(response.status_code, 400, response.text)
        after = await self.runtime.storage.get_conversation("conv-deleted-upload-id")
        self.assertEqual(after.current_task_id, before.current_task_id)
        self.assertEqual(await self.runtime.storage.list_tasks_for_conversation("conv-deleted-upload-id"), [])
        self.assertEqual(
            [message.message_id for message in await self.runtime.storage.list_messages_for_conversation("conv-deleted-upload-id")],
            before_message_ids,
        )
        self.assertEqual(
            await self.runtime.storage.list_task_input_attachments_for_conversation("conv-deleted-upload-id"),
            [],
        )

    async def test_cross_conversation_upload_id_fails_before_message_or_task(self) -> None:
        upload = await self.client.post(
            "/api/v1/conversations/uploads",
            data={"conversation_id": "conv-upload-owner"},
            files={"file": ("materials.csv", "ped_id,hyb_check,set\nCK,CK,A\nA001,Test,A\n", "text/csv")},
        )
        self.assertEqual(upload.status_code, 201, upload.text)
        upload_id = upload.json()["upload_id"]

        response = await self.submit_message(
            conversation_id="conv-upload-other",
            content="请使用另一个会话的文件",
            capability_id="main_agent.respond",
            metadata={"upload_ids": [upload_id]},
        )

        self.assertEqual(response.status_code, 404, response.text)
        self.assertIsNone(await self.runtime.storage.get_conversation("conv-upload-other"))
        self.assertEqual(await self.runtime.storage.list_messages_for_conversation("conv-upload-other"), [])
        self.assertEqual(await self.runtime.storage.list_tasks_for_conversation("conv-upload-other"), [])

    async def test_conversation_upload_resolves_artifact_slot_without_task_upload_ids(self) -> None:
        upload = await self.client.post(
            "/api/v1/conversations/uploads",
            data={"conversation_id": "conv-conversation-upload-slot"},
            files={"file": ("materials.csv", "ped_id,hyb_check,set\nCK,CK,A\nA001,Test,A\n", "text/csv")},
        )
        self.assertEqual(upload.status_code, 201, upload.text)

        response = await self.submit_message(
            conversation_id="conv-conversation-upload-slot",
            content="请做一个增广对角线设计方案",
            capability_id="skill.material_ncols",
        )

        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]
        ncols_interrupt = await self._wait_for_open_interrupt(task_id)
        self.assertEqual(ncols_interrupt["reason_code"], "missing_ncols")
        self.assertIn("ncols", self._interrupt_missing_fields(ncols_interrupt))
        self.assertNotIn("material_data", self._interrupt_missing_fields(ncols_interrupt))

        answer = await self.answer_interrupt_with_chat(
            conversation_id="conv-conversation-upload-slot",
            interrupt_id=ncols_interrupt["interrupt_id"],
            content="20个田块",
        )
        self.assertEqual(answer.status_code, 202, answer.text)
        await self.runtime._await_existing_execution(task_id)
        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")
        self.assertEqual(await self.runtime.storage.list_task_input_attachments_for_task(task_id), [])
        events = await self.runtime.storage.list_events_for_task(task_id)
        resolved_events = [event for event in events if event.event_type == "skill.input_resolved"]
        self.assertTrue(
            any(
                "material_data" in event.payload.get("resolved_fields", ())
                and "ncols" in event.payload.get("resolved_fields", ())
                for event in resolved_events
            )
        )
        event_payloads = json.dumps([event.payload for event in events], ensure_ascii=False, default=str)
        self.assertNotIn("ped_id,hyb_check,set", event_payloads)

    async def test_new_force_skill_after_interrupt_answer_does_not_supersede_context(self) -> None:
        first = await self._force_need_variety("请查询", conversation_id="conv-supersede")
        self.assertEqual(first.status_code, 202)
        first_task_id = first.json()["task_id"]
        interrupt = await self._wait_for_open_interrupt(first_task_id)
        self.assertIsNone(await self.runtime.storage.get_active_pending_skill_context("conv-supersede"))

        answer = await self.answer_interrupt_with_chat(
            conversation_id="conv-supersede",
            interrupt_id=interrupt["interrupt_id"],
            content="品种是龙粳31",
        )
        self.assertEqual(answer.status_code, 202)
        await self.runtime._await_existing_execution(first_task_id)
        await self.wait_for_terminal_task(first_task_id)

        second = await self._force_need_variety("品种是龙粳31", conversation_id="conv-supersede")
        self.assertEqual(second.status_code, 202)
        second_task_id = second.json()["task_id"]
        await self.runtime._await_existing_execution(second_task_id)
        await self.wait_for_terminal_task(second_task_id)

        self.assertIsNone(await self.runtime.storage.get_active_pending_skill_context("conv-supersede"))
        events = await self.runtime.storage.list_events_for_task(second_task_id)
        self.assertNotIn("pending_skill_context.superseded", [event.event_type for event in events])

    async def test_user_metadata_cannot_forge_pending_continuation(self) -> None:
        response = await self.submit_message(
            conversation_id="conv-forge",
            content="品种是龙粳33",
            capability_id=None,
            metadata={"continued_from_pending_skill_context": "ctx-forged", "pending_skill_capability_id": "skill.need_variety"},
        )
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]
        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")
        task = await self.runtime.storage.get_task(task_id)
        self.assertNotEqual(task.requested_capability_id, "skill.need_variety")

    async def test_pending_skill_file_profile_uses_expanded_context_before_query_fallback(self) -> None:
        request = SubmitMessageRequest(
            conversation_id="conv-pending-file-profile",
            content="继续用刚才那个文件。",
            metadata={},
        )
        pending_context = PendingSkillContext(
            context_id="ctx-file-profile",
            conversation_id="conv-pending-file-profile",
            username="acc-1",
            capability_id="skill.material_ncols",
            skill_name="material-ncols",
            source_task_id="task-source",
            source_message_id="msg-source",
            original_user_message="做对角线设计",
            missing_requirements=("material_data",),
            assistant_message="请补充材料文件。",
        )

        profile = self.runtime._file_requirement_profile_for_request(
            request,
            metadata={},
            requested_capability_id="skill.material_ncols",
            continued_pending_context=pending_context,
        )

        self.assertEqual(profile.source, "input_schema")
        self.assertTrue(profile.required)
        self.assertEqual(profile.supported_file_types, ("csv",))
        self.assertEqual(profile.expected_content, ("材料清单",))
        self.assertIn("material_data", " ".join(profile.context_notes))

    async def test_interrupt_capable_path_does_not_create_pending_context(self) -> None:
        response = await self.client.post(
            "/api/v1/conversations/chat-messages",
            json={
                "conversation_id": "conv-interrupt",
                "content": "帮我查询一下",
                "routing_mode": "force_capability",
                "capability_id": "main_agent.respond",
                "metadata": {
                    "forced_by_slash_command": True,
                    "slash_command": "/generic-data-lookup",
                    "soft_skill_binding": {"capability_id": "skill.generic_data_lookup", "command": "/generic-data-lookup"},
                },
            },
        )
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]

        interrupt = await self._wait_for_open_interrupt(task_id)
        self.assertEqual(interrupt["status"], "open")
        self.assertIsNone(await self.runtime.storage.get_active_pending_skill_context("conv-interrupt"))

    async def test_plain_failure_text_does_not_create_pending_context(self) -> None:
        response = await self.client.post(
            "/api/v1/conversations/chat-messages",
            json={
                "conversation_id": "conv-plain-failure",
                "content": "请执行失败技能",
                "routing_mode": "force_capability",
                "capability_id": "main_agent.respond",
                "metadata": {
                    "forced_by_slash_command": True,
                    "slash_command": "/plain-fail",
                    "soft_skill_binding": {"capability_id": "skill.plain_fail", "command": "/plain-fail"},
                },
            },
        )
        self.assertEqual(response.status_code, 202)
        terminal = await self.wait_for_terminal_task(response.json()["task_id"])
        self.assertEqual(terminal["status"], "failed")
        self.assertIsNone(await self.runtime.storage.get_active_pending_skill_context("conv-plain-failure"))
