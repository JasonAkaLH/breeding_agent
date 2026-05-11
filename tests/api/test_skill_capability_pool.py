from __future__ import annotations

import json

from tests.api.support import APITestCase


class SkillCapabilityPoolAPITest(APITestCase):
    async def test_fake_planner_can_select_project_skill_capability_and_force_main_agent_skill(self) -> None:
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
        prompts: list[str] = []

        def planner(prompt: str, **_kwargs) -> str:
            prompts.append(prompt)
            return json.dumps({"nodes": [{"node_id": "design", "capability_id": "skill.mini_breedstat_rcbd"}]})

        async def streamer(_prompt: str, **_kwargs):
            yield "done"

        await self.reconfigure_runtime(
            skill_roots=(project_skill_root,),
            public_skill_roots=(project_skill_root,),
            planner_text_generator=planner,
            main_agent_stream_generator=streamer,
        )

        response = await self.submit_message(content="请帮我处理这个材料表", capability_id=None)
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]
        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")

        self.assertIn("skill.mini_breedstat_rcbd", prompts[0])
        nodes = await self.runtime.storage.list_task_nodes_for_task(task_id)
        self.assertEqual([node.capability_id for node in nodes], ["main_agent.respond"])
        events = await self.runtime.storage.list_events_for_task(task_id)
        event_types = [event.event_type for event in events]
        self.assertIn("skill.forced_selected", event_types)
        self.assertIn("skill.matched", event_types)
        self.assertNotIn("skill.match_fallback", event_types)
        forced_selected = next(event for event in events if event.event_type == "skill.forced_selected")
        self.assertEqual(forced_selected.payload["source"], "planner")

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

        async def streamer(_prompt: str, **_kwargs):
            yield "done"

        await self.reconfigure_runtime(
            skill_roots=(project_skill_root,),
            public_skill_roots=(project_skill_root,),
            main_agent_stream_generator=streamer,
        )

        response = await self.submit_message(
            content="普通问候，不应强制使用 Skill",
            capability_id="main_agent.respond",
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
        self.assertIn("skill.match_fallback", event_types)
