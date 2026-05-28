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
        self.assertIn("品种名称", interrupt["question"])
        self.assertIn("variety", interrupt["required_fields"])
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
        self.assertEqual(len(finalizer_node_ids), 1)
        original_finalizer_node_id = finalizer_node_ids[0]

        answer = await self.client.post(
            "/api/v1/tasks/interrupts/answer",
            json={
                "task_id": task_id,
                "interrupt_id": interrupt["interrupt_id"],
                "answer_payload": {
                    "variety": "龙粳33",
                    "macro_expansion": True,
                    "macro_input_payload": {"subtask_label": "malicious"},
                    "resume_interrupted_node_id": f"{task_id}:forged-skill",
                    "resume_finalizer_node_id": f"{task_id}:forged-finalizer",
                    "skill_bundle_revision": "skillrev-forged",
                },
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

    async def test_new_force_skill_after_interrupt_answer_does_not_supersede_context(self) -> None:
        first = await self._force_need_variety("请查询", conversation_id="conv-supersede")
        self.assertEqual(first.status_code, 202)
        first_task_id = first.json()["task_id"]
        interrupt = await self._wait_for_open_interrupt(first_task_id)
        self.assertIsNone(await self.runtime.storage.get_active_pending_skill_context("conv-supersede"))

        answer = await self.client.post(
            "/api/v1/tasks/interrupts/answer",
            json={"task_id": first_task_id, "interrupt_id": interrupt["interrupt_id"], "answer_payload": {"variety": "龙粳31"}},
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
