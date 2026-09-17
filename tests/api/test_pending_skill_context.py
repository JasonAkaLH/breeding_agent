from __future__ import annotations

import json
import textwrap
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.api.dto import SubmitMessageRequest
from src.core.enums import InterruptStatus, MessageRole, NodeStatus, RoutingMode
from src.core.errors import MessageIdentityConflictError
from src.core.models import Conversation, Interrupt, Message, PendingSkillContext, Task, TaskNode
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
                "capability_id": "skill.need_variety",
                "metadata": {
                    "forced_by_slash_command": True,
                    "slash_command": "/need-variety",
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
        self.assertIsNone(terminal["root_node_id"])

        nodes = await self.runtime.storage.list_task_nodes_for_task(task_id)
        skill_nodes = [node for node in nodes if node.capability_id == "skill.need_variety"]
        self.assertEqual([node.node_id for node in skill_nodes], [interrupted_node_id])
        self.assertTrue(all(str(node.status) == "completed" for node in skill_nodes))
        self.assertFalse(
            any(node.node_id == f"{task_id}:skill_execute" for node in nodes if node.node_id != interrupted_node_id),
            "interrupt resume must reuse the interrupted dynamic Skill node instead of creating a second direct Skill node",
        )
        task = await self.runtime.storage.get_task(task_id)
        self.assertEqual(task.routing_mode, RoutingMode.FORCE_CAPABILITY)
        self.assertEqual(task.requested_capability_id, "skill.need_variety")
        self.assertIsNone(await self.runtime.storage.get_active_pending_skill_context("conv-pending"))
        events = await self.runtime.storage.list_events_for_task(task_id)
        self.assertIn("task.interrupt_answered", [event.event_type for event in events])
        self.assertIn("skill.execution_completed", [event.event_type for event in events])
        self.assertNotIn("pending_skill_context.consumed", [event.event_type for event in events])

    async def test_generic_interrupt_attachment_bind_failure_keeps_answer_open_and_retryable(self) -> None:
        conversation_id = "conv-generic-bind-failure"
        task_id = "task-generic-bind-failure"
        root_message_id = "root-generic-bind-failure"
        node_id = "node-generic-bind-failure"
        interrupt_id = "interrupt-generic-bind-failure"
        source_message_id = "answer-generic-bind-failure"
        await self.runtime.storage.save_conversation(
            Conversation(conversation_id=conversation_id, username="acc-1")
        )
        await self.runtime.storage.save_message(
            Message(
                message_id=root_message_id,
                conversation_id=conversation_id,
                role=MessageRole.USER,
                content="start",
            )
        )
        await self.runtime.storage.save_task(Task(task_id, conversation_id, root_message_id))
        skill_revision = self.runtime._skill_runtime_state.active_revision
        self.runtime._skill_runtime_state.retain_revision(skill_revision)
        self.runtime._task_skill_bundle_revisions[task_id] = skill_revision
        await self.runtime.storage.save_task_node(
            TaskNode(
                node_id=node_id,
                task_id=task_id,
                capability_id="skill.legacy",
                status=NodeStatus.WAITING_FOR_INPUT,
            )
        )
        await self.runtime.storage.save_interrupt(
            Interrupt(
                interrupt_id=interrupt_id,
                conversation_id=conversation_id,
                task_id=task_id,
                node_id=node_id,
                source_agent="skill.legacy",
                source_message_id=root_message_id,
                question="upload?",
                reason_code="legacy_missing_input",
                required_fields={"material": {"type": "artifact"}},
            )
        )
        payload = {"answer": "use upload", "upload_ids": ["upl-fault"]}

        with patch.object(
            self.runtime,
            "_bind_or_update_resume_input_attachments",
            new=AsyncMock(side_effect=RuntimeError("bind_failed")),
        ):
            with self.assertRaisesRegex(RuntimeError, "bind_failed"):
                await self.runtime.answer_interrupt(
                    task_id,
                    interrupt_id,
                    payload,
                    source_message_id=source_message_id,
                )

        interrupt_after_failure = await self.runtime.storage.get_interrupt(interrupt_id)
        self.assertIsNotNone(interrupt_after_failure)
        self.assertEqual(str(interrupt_after_failure.status), "open")
        self.assertEqual(await self.runtime.storage.list_interrupt_answers(interrupt_id), [])
        self.assertIsNone(await self.runtime.storage.get_message(source_message_id))

        with (
            patch.object(
                self.runtime,
                "_bind_or_update_resume_input_attachments",
                new=AsyncMock(return_value=None),
            ),
            patch.object(
                self.runtime,
                "_resume_agent_interrupt",
                new=AsyncMock(return_value=True),
            ),
        ):
            result = await self.runtime.answer_interrupt(
                task_id,
                interrupt_id,
                payload,
                source_message_id=source_message_id,
            )

        self.assertEqual(result["source_message_id"], source_message_id)
        self.assertIsNotNone(await self.runtime.storage.get_message(source_message_id))
        with (
            patch.object(
                self.runtime,
                "_bind_or_update_resume_input_attachments",
                new=AsyncMock(side_effect=AssertionError("replay must not rebind attachments")),
            ),
            patch.object(
                self.runtime,
                "_resume_agent_interrupt",
                new=AsyncMock(side_effect=AssertionError("replay must not resume twice")),
            ),
        ):
            replay = await self.runtime.answer_interrupt(
                task_id,
                interrupt_id,
                payload,
                source_message_id=source_message_id,
            )
        self.assertEqual(replay["source_message_id"], source_message_id)
        self.assertEqual(len(await self.runtime.storage.list_interrupt_answers(interrupt_id)), 1)

    async def test_remote_interrupt_open_partial_retry_repairs_control_before_replay(self) -> None:
        conversation_id = "conv-remote-replay-conflict"
        task_id = "task-remote-replay-conflict"
        root_message_id = "root-remote-replay-conflict"
        node_id = "node-remote-replay-conflict"
        interrupt_id = "interrupt-remote-replay-conflict"
        source_message_id = "answer-remote-replay-conflict"
        await self.runtime.storage.save_conversation(
            Conversation(conversation_id=conversation_id, username="acc-1")
        )
        await self.runtime.storage.save_message(
            Message(
                message_id=root_message_id,
                conversation_id=conversation_id,
                role=MessageRole.USER,
                content="start remote",
            )
        )
        await self.runtime.storage.save_task(Task(task_id, conversation_id, root_message_id))
        skill_revision = self.runtime._skill_runtime_state.active_revision
        self.runtime._skill_runtime_state.retain_revision(skill_revision)
        self.runtime._task_skill_bundle_revisions[task_id] = skill_revision
        await self.runtime.storage.save_task_node(
            TaskNode(
                node_id=node_id,
                task_id=task_id,
                capability_id="mcp.dispatch",
                status=NodeStatus.WAITING_FOR_INPUT,
            )
        )
        await self.runtime.storage.save_interrupt(
            Interrupt(
                interrupt_id=interrupt_id,
                conversation_id=conversation_id,
                task_id=task_id,
                node_id=node_id,
                source_agent="mcp.dispatch",
                source_message_id=root_message_id,
                question="remote input?",
                reason_code="mcp_remote_task_input_required",
            )
        )
        payload = {"mcp_input_responses": {"region": "north"}}

        async def record_control(answer, **_kwargs):
            await self.runtime.storage.save_interrupt_answer(answer)
            if control.await_count == 1:
                raise RuntimeError("control_write_failed")
            current_interrupt = await self.runtime.storage.get_interrupt(interrupt_id)
            assert current_interrupt is not None
            await self.runtime.storage.save_interrupt(
                replace(
                    current_interrupt,
                    status=InterruptStatus.ANSWERED,
                    answered_at=answer.accepted_at,
                )
            )
            current_node = await self.runtime.storage.get_task_node(node_id)
            assert current_node is not None
            await self.runtime.storage.save_task_node(
                replace(current_node, status=NodeStatus.WAITING_FOR_DEPENDENCY)
            )
            return SimpleNamespace(kind="update")

        control = AsyncMock(side_effect=record_control)
        with patch.object(
            self.runtime.interrupt_service,
            "record_mcp_remote_task_control",
            new=control,
        ):
            with self.assertRaisesRegex(RuntimeError, "control_write_failed"):
                await self.runtime.answer_interrupt(
                    task_id,
                    interrupt_id,
                    payload,
                    source_message_id=source_message_id,
                )
            replay = await self.runtime.answer_interrupt(
                task_id,
                interrupt_id,
                payload,
                source_message_id=source_message_id,
            )
            exact_replay = await self.runtime.answer_interrupt(
                task_id,
                interrupt_id,
                payload,
                source_message_id=source_message_id,
            )
            with self.assertRaises(MessageIdentityConflictError):
                await self.runtime.answer_interrupt(
                    task_id,
                    interrupt_id,
                    {"mcp_input_responses": {"region": "south"}},
                    source_message_id=source_message_id,
                )
            with self.assertRaises(MessageIdentityConflictError):
                await self.runtime.answer_interrupt(
                    task_id,
                    interrupt_id,
                    payload,
                    source_message_id="answer-remote-replay-other-source",
                )

        self.assertEqual(replay["action"], "mcp_remote_task_input_submitted")
        self.assertEqual(exact_replay, replay)
        self.assertEqual(control.await_count, 2)

    async def test_generic_interrupt_partial_attachment_retry_uses_final_deterministic_answer_id(self) -> None:
        conversation_id = "conv-generic-partial-bind"
        task_id = "task-generic-partial-bind"
        root_message_id = "root-generic-partial-bind"
        node_id = "node-generic-partial-bind"
        interrupt_id = "interrupt-generic-partial-bind"
        source_message_id = "answer-generic-partial-bind"
        await self.runtime.storage.save_conversation(
            Conversation(conversation_id=conversation_id, username="acc-1")
        )
        upload_ids: list[str] = []
        for index in (1, 2):
            upload = await self.client.post(
                "/api/v1/conversations/uploads",
                data={"conversation_id": conversation_id},
                files={
                    "file": (
                        f"materials-{index}.csv",
                        f"ped_id,value\nA00{index},{index}\n",
                        "text/csv",
                    )
                },
            )
            self.assertEqual(upload.status_code, 201, upload.text)
            upload_ids.append(upload.json()["upload_id"])
        await self.runtime.storage.save_message(
            Message(
                message_id=root_message_id,
                conversation_id=conversation_id,
                role=MessageRole.USER,
                content="start partial bind",
            )
        )
        await self.runtime.storage.save_task(Task(task_id, conversation_id, root_message_id))
        skill_revision = self.runtime._skill_runtime_state.active_revision
        self.runtime._skill_runtime_state.retain_revision(skill_revision)
        self.runtime._task_skill_bundle_revisions[task_id] = skill_revision
        await self.runtime.storage.save_task_node(
            TaskNode(
                node_id=node_id,
                task_id=task_id,
                capability_id="skill.legacy",
                status=NodeStatus.WAITING_FOR_INPUT,
            )
        )
        await self.runtime.storage.save_interrupt(
            Interrupt(
                interrupt_id=interrupt_id,
                conversation_id=conversation_id,
                task_id=task_id,
                node_id=node_id,
                source_agent="skill.legacy",
                source_message_id=root_message_id,
                question="uploads?",
                reason_code="legacy_missing_input",
                required_fields={"materials": {"type": "artifact"}},
            )
        )
        payload = {"answer": "use both", "upload_ids": upload_ids}
        expected_answer_id = self.runtime._interrupt_answer_id(
            interrupt_id,
            source_message_id,
        )
        original_save_attachment = self.runtime.storage.save_task_input_attachment
        save_count = 0

        async def fail_second_attachment(attachment):
            nonlocal save_count
            save_count += 1
            if save_count == 2:
                raise RuntimeError("second_attachment_failed")
            return await original_save_attachment(attachment)

        with patch.object(
            self.runtime.storage,
            "save_task_input_attachment",
            side_effect=fail_second_attachment,
        ):
            with self.assertRaisesRegex(RuntimeError, "second_attachment_failed"):
                await self.runtime.answer_interrupt(
                    task_id,
                    interrupt_id,
                    payload,
                    source_message_id=source_message_id,
                )
        partial = await self.runtime.storage.list_task_input_attachments_for_task(task_id)
        self.assertEqual(len(partial), 1)
        self.assertEqual(partial[0].interrupt_answer_id, expected_answer_id)

        with (
            patch.object(
                self.runtime,
                "_resume_agent_interrupt",
                new=AsyncMock(return_value=False),
            ),
            patch.object(
                self.runtime,
                "_schedule_execution",
                new=AsyncMock(side_effect=RuntimeError("durable_init_failed")),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "durable_init_failed"):
                await self.runtime.answer_interrupt(
                    task_id,
                    interrupt_id,
                    payload,
                    source_message_id=source_message_id,
                )
        self.assertEqual(
            [
                event
                for event in await self.runtime.storage.list_events_for_task(task_id)
                if event.event_type == "task.interrupt_continuation_completed"
            ],
            [],
        )

        scheduler = AsyncMock(return_value=None)
        with (
            patch.object(
                self.runtime,
                "_resume_agent_interrupt",
                new=AsyncMock(return_value=False),
            ),
            patch.object(self.runtime, "_schedule_execution", new=scheduler),
        ):
            await self.runtime.answer_interrupt(
                task_id,
                interrupt_id,
                payload,
                source_message_id=source_message_id,
            )
        scheduled_request = scheduler.await_args.args[0]
        self.assertEqual(scheduled_request.user_message.count("use both"), 1)

        attachments = await self.runtime.storage.list_task_input_attachments_for_task(task_id)
        self.assertEqual(len(attachments), 2)
        self.assertEqual(
            {attachment.interrupt_answer_id for attachment in attachments},
            {expected_answer_id},
        )
        answers = await self.runtime.storage.list_interrupt_answers(interrupt_id)
        self.assertEqual([answer.interrupt_answer_id for answer in answers], [expected_answer_id])

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
            capability_id=None,
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
            capability_id=None,
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
            capability_id=None,
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
            capability_id=None,
            metadata={"upload_ids": [upload_id]},
        )

        self.assertEqual(response.status_code, 404, response.text)
        self.assertIsNone(await self.runtime.storage.get_conversation("conv-upload-other"))
        self.assertEqual(await self.runtime.storage.list_messages_for_conversation("conv-upload-other"), [])
        self.assertEqual(await self.runtime.storage.list_tasks_for_conversation("conv-upload-other"), [])

    async def test_conversation_upload_does_not_resolve_artifact_slot_without_task_attachment(self) -> None:
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
        skill_interrupt = await self._wait_for_open_interrupt(task_id)
        self.assertEqual(skill_interrupt["reason_code"], "missing_skill_input")
        self.assertIn(
            "material_data", self._interrupt_missing_fields(skill_interrupt)
        )
        self.assertIn("ncols", self._interrupt_missing_fields(skill_interrupt))
        self.assertEqual(await self.runtime.storage.list_task_input_attachments_for_task(task_id), [])

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
        transition = next(
            event
            for event in events
            if event.event_type == "pending_skill_context.superseded"
        )
        self.assertEqual(transition.payload["count"], 0)

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
                "capability_id": "skill.generic_data_lookup",
                "metadata": {
                    "forced_by_slash_command": True,
                    "slash_command": "/generic-data-lookup",
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
                "capability_id": "skill.plain_fail",
                "metadata": {
                    "forced_by_slash_command": True,
                    "slash_command": "/plain-fail",
                },
            },
        )
        self.assertEqual(response.status_code, 202)
        terminal = await self.wait_for_terminal_task(response.json()["task_id"])
        self.assertEqual(terminal["status"], "completed")
        self.assertIsNone(await self.runtime.storage.get_active_pending_skill_context("conv-plain-failure"))
