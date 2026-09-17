from __future__ import annotations

import textwrap

from tests.api.support import APITestCase


class LegacyProjectSkillCompatibilityAPITest(APITestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.legacy_root = self.workspace / "legacy-slot-skills"
        self._write_legacy_parameter_skill()
        await self.reconfigure_runtime(
            skill_roots=(self.legacy_root,),
            public_skill_roots=(self.legacy_root,),
            enable_skill_input_llm=False,
        )

    def _write_legacy_parameter_skill(self) -> None:
        skill_dir = self.legacy_root / "legacy-slot"
        scripts = skill_dir / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "answer.py").write_text(
            "import json, sys\npayload=json.load(sys.stdin)\nprint(json.dumps({'answer': payload}, ensure_ascii=False))\n",
            encoding="utf-8",
        )
        (skill_dir / "SKILL.md").write_text(
            textwrap.dedent(
                """
                ---
                name: legacy-slot
                capability_id: skill.legacy_slot
                description: legacy manifest-parameter Skill without v2 contract
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
                    aliases: [blocks, 重复]
                ---

                # Legacy slot
                """
            ).strip(),
            encoding="utf-8",
        )

    async def test_no_contract_project_skill_is_rejected_without_execution(self) -> None:
        response = await self.submit_message(
            conversation_id="conv-legacy-fail-closed",
            content="请执行旧参数 Skill",
            capability_id="skill.legacy_slot",
        )

        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("Unsupported capability_id", response.text)
        self.assertIsNone(await self.runtime.storage.get_active_pending_skill_context("conv-legacy-fail-closed"))

    async def test_no_contract_project_skill_does_not_create_legacy_slot_collection(self) -> None:
        response = await self.submit_message(
            conversation_id="conv-legacy-no-slot-state",
            content="blocks=3",
            capability_id="skill.legacy_slot",
        )

        self.assertEqual(response.status_code, 400, response.text)
        tasks = await self.runtime.storage.list_tasks_for_conversation("conv-legacy-no-slot-state")
        for task in tasks:
            collections = await self.runtime.storage.list_slot_collections_for_task(task.task_id)
            self.assertEqual(collections, [])
