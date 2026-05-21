from __future__ import annotations

import textwrap

from src.core.enums import RoutingMode, TaskStatus
from tests.api.support import APITestCase


class PendingSkillContextAPITest(APITestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        root = self.workspace / "pending-skills"
        self._write_need_variety_skill(root)
        self._write_plain_fail_skill(root)
        await self.reconfigure_runtime(
            skill_roots=(self.default_project_skill_root, root),
            public_skill_roots=(self.default_project_skill_root, root),
            enable_skill_input_llm=False,
        )

    def _write_need_variety_skill(self, root) -> None:
        skill_dir = root / "need-variety"
        scripts = skill_dir / "scripts"
        scripts.mkdir(parents=True)
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
capability_id: skill.need_variety
description: 需要品种名的测试 Skill
scripts:
  - name: answer
    path: scripts/answer.py
    runtime: python
    auto_run: true
    inputs:
      required:
        - query
outputs:
  required:
    - answer
parameters:
  variety:
    type: string
    required: true
    aliases:
      - 品种
    patterns:
      - '品种(?:是|为)?([\\w\\u4e00-\\u9fff]+)'
---

# Need Variety
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
capability_id: skill.plain_fail
description: 返回普通失败的测试 Skill
scripts:
  - name: fail
    path: scripts/fail.py
    runtime: python
    auto_run: true
    inputs:
      required:
        - query
outputs:
  required:
    - answer
---

# Plain Fail
""",
            encoding="utf-8",
        )

    async def _force_need_variety(self, content: str, conversation_id: str = "conv-pending"):
        return await self.client.post(
            "/api/v1/conversations/chat-messages",
            json={
                "conversation_id": conversation_id,
                "account_id": "acc-1",
                "content": content,
                "routing_mode": "force_capability",
                "capability_id": "skill.need_variety",
                "metadata": {"forced_by_slash_command": True, "slash_command": "/need-variety"},
            },
        )

    async def test_force_skill_missing_input_creates_persistent_context_and_allows_next_message(self) -> None:
        first = await self._force_need_variety("请查询")
        self.assertEqual(first.status_code, 202)
        first_task_id = first.json()["task_id"]
        first_terminal = await self.wait_for_terminal_task(first_task_id)
        self.assertEqual(first_terminal["status"], "completed")

        context = await self.runtime.storage.get_active_pending_skill_context("conv-pending")
        self.assertIsNotNone(context)
        self.assertEqual(context.capability_id, "skill.need_variety")
        self.assertEqual(context.missing_requirements, ("variety",))
        self.assertEqual(context.original_user_message, "请查询")
        events = await self.runtime.storage.list_events_for_task(first_task_id)
        self.assertIn("pending_skill_context.created", [event.event_type for event in events])

        messages = await self.runtime.storage.list_messages_for_conversation("conv-pending")
        self.assertTrue(any(message.role == "assistant" and "variety" in message.content for message in messages))
        conversation = await self.runtime.storage.get_conversation("conv-pending")
        self.assertIsNone(conversation.current_task_id)

    async def test_next_plain_message_continues_pending_skill_and_consumes_context(self) -> None:
        first = await self._force_need_variety("请查询")
        self.assertEqual(first.status_code, 202)
        await self.wait_for_terminal_task(first.json()["task_id"])
        context = await self.runtime.storage.get_active_pending_skill_context("conv-pending")
        self.assertIsNotNone(context)

        second = await self.submit_message(conversation_id="conv-pending", content="品种是龙粳33", capability_id=None)
        self.assertEqual(second.status_code, 202)
        task_id = second.json()["task_id"]
        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")

        task = await self.runtime.storage.get_task(task_id)
        self.assertEqual(task.routing_mode, RoutingMode.AUTO)
        self.assertEqual(task.requested_capability_id, "skill.need_variety")
        consumed = await self.runtime.storage.get_pending_skill_context(context.context_id)
        self.assertEqual(consumed.status, "consumed")
        self.assertIsNone(await self.runtime.storage.get_active_pending_skill_context("conv-pending"))
        events = await self.runtime.storage.list_events_for_task(task_id)
        self.assertIn("skill.execution_completed", [event.event_type for event in events])
        self.assertIn("pending_skill_context.consumed", [event.event_type for event in events])
        plan = next(event for event in events if event.event_type == "workflow.plan_built")
        self.assertEqual(plan.payload["metadata"]["continued_from_pending_skill_context"], context.context_id)

    async def test_new_force_skill_supersedes_existing_pending_context(self) -> None:
        first = await self._force_need_variety("请查询", conversation_id="conv-supersede")
        self.assertEqual(first.status_code, 202)
        await self.wait_for_terminal_task(first.json()["task_id"])
        old = await self.runtime.storage.get_active_pending_skill_context("conv-supersede")
        self.assertIsNotNone(old)

        second = await self._force_need_variety("品种是龙粳31", conversation_id="conv-supersede")
        self.assertEqual(second.status_code, 202)
        await self.wait_for_terminal_task(second.json()["task_id"])

        superseded = await self.runtime.storage.get_pending_skill_context(old.context_id)
        self.assertEqual(superseded.status, "superseded")
        self.assertIsNone(await self.runtime.storage.get_active_pending_skill_context("conv-supersede"))
        events = await self.runtime.storage.list_events_for_task(second.json()["task_id"])
        self.assertIn("pending_skill_context.superseded", [event.event_type for event in events])

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

    async def test_interrupt_capable_path_does_not_create_pending_context(self) -> None:
        response = await self.client.post(
            "/api/v1/conversations/chat-messages",
            json={
                "conversation_id": "conv-interrupt",
                "account_id": "acc-1",
                "content": "帮我查询一下",
                "routing_mode": "force_capability",
                "capability_id": "skill.generic_data_lookup",
                "metadata": {"forced_by_slash_command": True, "slash_command": "/generic-data-lookup"},
            },
        )
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]

        async def task_waiting() -> bool:
            task = await self.runtime.storage.get_task(task_id)
            return task is not None and task.status == TaskStatus.RUNNING

        await self.wait_for_condition(task_waiting)
        self.assertIsNone(await self.runtime.storage.get_active_pending_skill_context("conv-interrupt"))

    async def test_plain_failure_text_does_not_create_pending_context(self) -> None:
        response = await self.client.post(
            "/api/v1/conversations/chat-messages",
            json={
                "conversation_id": "conv-plain-failure",
                "account_id": "acc-1",
                "content": "请执行失败技能",
                "routing_mode": "force_capability",
                "capability_id": "skill.plain_fail",
                "metadata": {"forced_by_slash_command": True, "slash_command": "/plain-fail"},
            },
        )
        self.assertEqual(response.status_code, 202)
        terminal = await self.wait_for_terminal_task(response.json()["task_id"])
        self.assertEqual(terminal["status"], "failed")
        self.assertIsNone(await self.runtime.storage.get_active_pending_skill_context("conv-plain-failure"))
