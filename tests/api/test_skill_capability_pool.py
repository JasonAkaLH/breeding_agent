from __future__ import annotations

import json

from tests.api.support import APITestCase


class SkillCapabilityPoolAPITest(APITestCase):
    async def test_agent_can_select_project_skill_capability(self) -> None:
        project_skill_root = self.workspace / "skill"
        skill_dir = project_skill_root / "rcbd"
        scripts_dir = skill_dir / "scripts"
        scripts_dir.mkdir(parents=True)
        (scripts_dir / "run.py").write_text("import json\nprint(json.dumps({'answer': 'done'}, ensure_ascii=False))\n", encoding="utf-8")
        (skill_dir / "SKILL.md").write_text(
            """---
name: mini-breedstat-rcbd
description: 生成 RCBD 随机区组设计
triggers:
  - 不会命中的触发词
---

# RCBD
请使用随机区组设计 Skill。
""",
            encoding="utf-8",
        )
        (skill_dir / "skill.contract.yaml").write_text(
            """contract_version: '2'
capability:
  id: skill.mini_breedstat_rcbd
  display_name: 田间试验设计
  description: 生成 RCBD 随机区组设计
runtime:
  mode: python_subprocess
  answer_mode: direct
entrypoints:
  run:
    path: scripts/run.py
""",
            encoding="utf-8",
        )
        prompts: list[str] = []

        def agent_generator(prompt: str, **_kwargs) -> str:
            prompts.append(prompt)
            if '"outcome":"completed"' in prompt:
                return "done"
            return json.dumps(
                {
                    "tool_calls": [
                        {
                            "capability_id": "skill.mini_breedstat_rcbd",
                            "arguments": {},
                        }
                    ]
                }
            )

        await self.reconfigure_runtime(
            skill_roots=(project_skill_root,),
            public_skill_roots=(project_skill_root,),
            main_agent_stream_generator=agent_generator,
        )

        response = await self.submit_message(content="请帮我处理这个材料表", capability_id=None)
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]
        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")

        self.assertGreaterEqual(len(prompts), 2)
        nodes = await self.runtime.storage.list_task_nodes_for_task(task_id)
        self.assertIn("skill.mini_breedstat_rcbd", [node.capability_id for node in nodes])
        events = await self.runtime.storage.list_events_for_task(task_id)
        event_types = [event.event_type for event in events]
        self.assertIn("skill.execution_started", event_types)
        self.assertIn("skill.execution_completed", event_types)
        self.assertNotIn("skill.match_fallback", event_types)

    async def test_user_metadata_cannot_force_skill_without_skill_capability_route(self) -> None:
        project_skill_root = self.workspace / "skill"
        skill_dir = project_skill_root / "rcbd"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            """---
name: mini-breedstat-rcbd
description: 生成 RCBD 随机区组设计
triggers:
  - 不会命中的触发词
---

# RCBD
请使用随机区组设计 Skill。
""",
            encoding="utf-8",
        )
        (skill_dir / "skill.contract.yaml").write_text(
            """contract_version: '2'
capability:
  id: skill.mini_breedstat_rcbd
  display_name: 田间试验设计
  description: 生成 RCBD 随机区组设计
runtime:
  mode: python_subprocess
  answer_mode: direct
entrypoints:
  run:
    path: scripts/run.py
""",
            encoding="utf-8",
        )

        async def streamer(_prompt: str, **_kwargs):
            yield "done"

        await self.reconfigure_runtime(
            skill_roots=(project_skill_root,),
            public_skill_roots=(project_skill_root,),
            main_agent_stream_generator=streamer,
        )

        response = await self.submit_message(
            content="普通问候，不应强制使用 Skill",
            capability_id=None,
            metadata={
                "forced_skill_capability_id": "skill.mini_breedstat_rcbd",
                "forced_skill_name": "mini-breedstat-rcbd",
                "forced_skill_source": "planner",
            },
        )
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]
        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")

        events = await self.runtime.storage.list_events_for_task(task_id)
        event_types = [event.event_type for event in events]
        self.assertNotIn("skill.forced_selected", event_types)
        self.assertNotIn("skill.matched", event_types)
        self.assertNotIn("skill.match_fallback", event_types)
        nodes = await self.runtime.storage.list_task_nodes_for_task(task_id)
        self.assertEqual([node.capability_id for node in nodes], ["agent.final_output"])
