from __future__ import annotations

import textwrap

from src.integrations.agent_skills.missing_input_interrupt import SLOT_COLLECTION_FIELD
from tests.api.support import APITestCase


class SkillSlotCollectionAPITest(APITestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.skill_root = self.workspace / "slot-skills"
        self._write_two_scalar_skill()
        self._write_one_scalar_skill()
        self._write_artifact_skill()
        self._write_plain_fail_skill()
        await self.reconfigure_runtime(
            skill_roots=(self.default_project_skill_root, self.skill_root),
            public_skill_roots=(self.default_project_skill_root, self.skill_root),
            enable_skill_input_llm=False,
        )

    def _write_two_scalar_skill(self) -> None:
        skill_dir = self.skill_root / "slot-two-scalar"
        scripts = skill_dir / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "answer.py").write_text(
            textwrap.dedent(
                """
                import json
                import sys

                payload = json.load(sys.stdin)
                print(json.dumps({
                    "answer": f"blocks={payload.get('blocks')}, ncols={payload.get('ncols')}",
                    "blocks": payload.get("blocks"),
                    "ncols": payload.get("ncols"),
                }, ensure_ascii=False))
                """
            ).strip(),
            encoding="utf-8",
        )
        (skill_dir / "SKILL.md").write_text(
            """---
name: slot-two-scalar
capability_id: skill.slot_two_scalar
description: 需要两个整数参数的补槽测试 Skill
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
  blocks:
    type: integer
    required: true
    aliases:
      - blocks
      - 重复
    patterns:
      - '(?:blocks|重复)\\s*[=：:]?\\s*(\\d+)'
      - '(\\d+)\\s*(?:个|次)?重复'
  ncols:
    type: integer
    required: true
    aliases:
      - ncols
      - 田块列数
      - 列数
    patterns:
      - '(?:ncols|田块列数|列数)\\s*[=：:]?\\s*(\\d+)'
      - '(\\d+)\\s*(?:列|个田块)'
---

# Slot two scalar
""",
            encoding="utf-8",
        )

    def _write_one_scalar_skill(self) -> None:
        skill_dir = self.skill_root / "slot-one-scalar"
        scripts = skill_dir / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "answer.py").write_text(
            "import json, sys\npayload=json.load(sys.stdin)\nprint(json.dumps({'answer': 'blocks=' + str(payload.get('blocks'))}, ensure_ascii=False))\n",
            encoding="utf-8",
        )
        (skill_dir / "SKILL.md").write_text(
            """---
name: slot-one-scalar
capability_id: skill.slot_one_scalar
description: 需要一个整数参数的补槽测试 Skill
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
  blocks:
    type: integer
    required: true
    aliases:
      - blocks
      - 重复
---

# Slot one scalar
""",
            encoding="utf-8",
        )

    def _write_artifact_skill(self) -> None:
        skill_dir = self.skill_root / "slot-artifact"
        scripts = skill_dir / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "answer.py").write_text(
            "import json, sys\npayload=json.load(sys.stdin)\nprint(json.dumps({'answer': 'material ok', 'material': payload.get('material_data')}, ensure_ascii=False))\n",
            encoding="utf-8",
        )
        (skill_dir / "SKILL.md").write_text(
            """---
name: slot-artifact
capability_id: skill.slot_artifact
description: 需要材料文件的补槽测试 Skill
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
  material_data:
    type: artifact
    required: true
    aliases:
      - material_data
      - 材料清单
---

# Slot artifact
""",
            encoding="utf-8",
        )

    def _write_plain_fail_skill(self) -> None:
        skill_dir = self.skill_root / "slot-plain-fail"
        scripts = skill_dir / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "fail.py").write_text("import sys\nprint('plain failure', file=sys.stderr)\nsys.exit(2)\n", encoding="utf-8")
        (skill_dir / "SKILL.md").write_text(
            """---
name: slot-plain-fail
capability_id: skill.slot_plain_fail
description: 普通失败补槽负向测试 Skill
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

# Slot plain fail
""",
            encoding="utf-8",
        )

    async def _open_interrupt(self, task_id: str, *, exclude: set[str] | None = None) -> dict[str, object]:
        exclude = exclude or set()
        await self.runtime._await_existing_execution(task_id)

        async def has_open_interrupt() -> bool:
            interrupts = await self.runtime.storage.list_interrupts_for_task(task_id)
            return any(str(interrupt.status) == "open" and interrupt.interrupt_id not in exclude for interrupt in interrupts)

        await self.wait_for_condition(has_open_interrupt)
        interrupts = await self.runtime.list_interrupts(task_id)
        return next(
            interrupt
            for interrupt in interrupts
            if interrupt["status"] == "open" and interrupt["interrupt_id"] not in exclude
        )

    async def test_multi_round_scalar_slot_collection_preserves_resolved_values_and_completes(self) -> None:
        response = await self.submit_message(
            conversation_id="conv-slot-rounds",
            content="请生成试验设计",
            capability_id="skill.slot_two_scalar",
        )
        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]

        first_interrupt = await self._open_interrupt(task_id)
        first_collection = first_interrupt["required_fields"][SLOT_COLLECTION_FIELD]
        self.assertEqual(first_collection["round"], 1)
        self.assertEqual(set(first_collection["missing"]), {"blocks", "ncols"})
        self.assertNotIn("请在输入框补充后继续当前任务", first_interrupt["question"])

        answer = await self.client.post(
            "/api/v1/tasks/interrupts/answer",
            json={
                "task_id": task_id,
                "interrupt_id": first_interrupt["interrupt_id"],
                "answer_payload": {"blocks": "3"},
            },
        )
        self.assertEqual(answer.status_code, 202, answer.text)

        second_interrupt = await self._open_interrupt(task_id, exclude={first_interrupt["interrupt_id"]})
        second_collection = second_interrupt["required_fields"][SLOT_COLLECTION_FIELD]
        self.assertNotEqual(second_interrupt["interrupt_id"], first_interrupt["interrupt_id"])
        self.assertEqual(second_collection["round"], 2)
        self.assertEqual(second_collection["resolved"]["blocks"], 3)
        self.assertEqual(second_collection["missing"], ["ncols"])
        self.assertIn("区组数/重复数已收到", second_interrupt["question"])
        self.assertIn("田块列数", second_interrupt["question"])

        second_answer = await self.client.post(
            "/api/v1/tasks/interrupts/answer",
            json={
                "task_id": task_id,
                "interrupt_id": second_interrupt["interrupt_id"],
                "answer_payload": {"ncols": "10"},
            },
        )
        self.assertEqual(second_answer.status_code, 202, second_answer.text)
        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")

        interrupts = await self.runtime.list_interrupts(task_id)
        self.assertFalse(any(interrupt["status"] == "open" for interrupt in interrupts), interrupts)
        events = await self.runtime.storage.list_events_for_task(task_id)
        resolved_events = [event for event in events if event.event_type == "skill.input_resolved"]
        self.assertTrue(
            any({"blocks", "ncols"}.issubset(set(event.payload.get("resolved_fields", ()))) for event in resolved_events),
            [event.payload for event in resolved_events],
        )
        waiting_events = [event for event in events if event.event_type == "node.waiting_for_input"]
        self.assertTrue(any(event.payload.get("round") == 1 for event in waiting_events))
        self.assertTrue(any(event.payload.get("round") == 2 for event in waiting_events))

    async def test_invalid_scalar_answer_keeps_task_waiting_for_next_round(self) -> None:
        response = await self.submit_message(
            conversation_id="conv-slot-invalid",
            content="请生成试验设计",
            capability_id="skill.slot_one_scalar",
        )
        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]
        first_interrupt = await self._open_interrupt(task_id)

        answer = await self.client.post(
            "/api/v1/tasks/interrupts/answer",
            json={
                "task_id": task_id,
                "interrupt_id": first_interrupt["interrupt_id"],
                "answer_payload": {"blocks": "零"},
            },
        )
        self.assertEqual(answer.status_code, 202, answer.text)

        second_interrupt = await self._open_interrupt(task_id, exclude={first_interrupt["interrupt_id"]})
        collection = second_interrupt["required_fields"][SLOT_COLLECTION_FIELD]
        self.assertEqual(collection["round"], 2)
        self.assertEqual(collection["missing"], ["blocks"])
        self.assertEqual(collection["no_progress_rounds"], 1)
        self.assertEqual(collection["slots"][0]["last_validation_error"], "invalid_value")
        self.assertEqual(collection["validation_errors"], {"blocks": "invalid_value"})
        events = await self.runtime.storage.list_events_for_task(task_id)
        self.assertTrue(
            any(
                event.event_type == "node.waiting_for_input"
                and event.payload.get("round") == 2
                and event.payload.get("validation_errors") == {"blocks": "invalid_value"}
                for event in events
            )
        )
        task_payload = (await self.client.get(f"/api/v1/tasks/{task_id}")).json()
        self.assertNotIn(task_payload["status"], {"completed", "failed", "cancelled"})

    async def test_artifact_slot_cannot_be_satisfied_by_text_payload(self) -> None:
        response = await self.submit_message(
            conversation_id="conv-slot-artifact",
            content="请生成试验设计",
            capability_id="skill.slot_artifact",
        )
        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]
        first_interrupt = await self._open_interrupt(task_id)
        self.assertIs(first_interrupt["required_fields"]["material_data"]["accepts_upload"], True)

        answer = await self.client.post(
            "/api/v1/tasks/interrupts/answer",
            json={
                "task_id": task_id,
                "interrupt_id": first_interrupt["interrupt_id"],
                "answer_payload": {"material_data": "/tmp/fake.csv"},
            },
        )
        self.assertEqual(answer.status_code, 202, answer.text)

        second_interrupt = await self._open_interrupt(task_id, exclude={first_interrupt["interrupt_id"]})
        collection = second_interrupt["required_fields"][SLOT_COLLECTION_FIELD]
        self.assertEqual(collection["round"], 2)
        self.assertEqual(collection["missing"], ["material_data"])
        self.assertEqual(collection["validation_errors"], {"material_data": "invalid_artifact_source"})
        self.assertIs(second_interrupt["required_fields"]["material_data"]["accepts_upload"], True)
        self.assertNotIn("/tmp/fake.csv", repr(collection))

    async def test_typed_missing_input_does_not_create_pending_context_or_old_assistant_text(self) -> None:
        response = await self.submit_message(
            conversation_id="conv-slot-no-pending",
            content="请生成试验设计",
            capability_id="skill.slot_one_scalar",
        )
        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]
        interrupt = await self._open_interrupt(task_id)

        self.assertIn(SLOT_COLLECTION_FIELD, interrupt["required_fields"])
        self.assertIsNone(await self.runtime.storage.get_active_pending_skill_context("conv-slot-no-pending"))
        messages = await self.runtime.storage.list_messages_for_conversation("conv-slot-no-pending")
        self.assertNotIn("缺少 Skill 必需信息", "\n".join(message.content for message in messages))

    async def test_plain_failure_does_not_create_slot_collection(self) -> None:
        response = await self.submit_message(
            conversation_id="conv-slot-plain-fail",
            content="请执行失败 Skill",
            capability_id="skill.slot_plain_fail",
        )
        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]
        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "failed")
        interrupts = await self.runtime.list_interrupts(task_id)
        self.assertFalse(
            any(SLOT_COLLECTION_FIELD in interrupt.get("required_fields", {}) for interrupt in interrupts),
            interrupts,
        )
        self.assertIsNone(await self.runtime.storage.get_active_pending_skill_context("conv-slot-plain-fail"))
