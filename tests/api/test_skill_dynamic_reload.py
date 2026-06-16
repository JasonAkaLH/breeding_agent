from __future__ import annotations

import json
from unittest.mock import patch

from tests.api.support import APITestCase


class SkillDynamicReloadAPITest(APITestCase):
    def _write_skill(self, root, name="demo-hot-reload", description="动态加载 Skill") -> None:
        skill_dir = root / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(
            f"""---
name: {name}
description: {description}
---

# Demo
请使用动态加载 Skill。
""",
            encoding="utf-8",
        )
        capability_id = "skill." + name.replace("-", "_")
        (skill_dir / "skill.contract.yaml").write_text(
            f"""contract_version: '2'
capability:
  id: {capability_id}
  display_name: {description}
routing:
  triggers:
    - 动态加载
runtime:
  mode: delegated_main_agent
  answer_mode: direct
entrypoints:
  run:
    runtime: delegated_main_agent
""",
            encoding="utf-8",
        )

    async def test_new_conversation_refreshes_added_skill_without_runtime_rebuild(self) -> None:
        project_skill_root = self.workspace / "skill"
        project_skill_root.mkdir(parents=True)
        prompts: list[str] = []

        def planner(prompt: str, **_kwargs) -> str:
            prompts.append(prompt)
            return json.dumps({"nodes": [{"node_id": "demo", "capability_id": "skill.demo_hot_reload"}]})

        async def streamer(_prompt: str, **_kwargs):
            yield "done"

        await self.reconfigure_runtime(
            skill_roots=(project_skill_root,),
            public_skill_roots=(project_skill_root,),
            planner_text_generator=planner,
            main_agent_stream_generator=streamer,
        )
        before = await self.client.get("/api/v1/capabilities")
        self.assertNotIn("skill.demo_hot_reload", {item["capability_id"] for item in before.json()["capabilities"]})

        self._write_skill(project_skill_root)
        response = await self.submit_message(conversation_id="conv-hot", content="请处理动态加载任务", capability_id=None)
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]
        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")

        self.assertIn("skill.demo_hot_reload", prompts[0])
        nodes = await self.runtime.storage.list_task_nodes_for_task(task_id)
        self.assertEqual([node.capability_id for node in nodes], ["main_agent.respond"])
        events = await self.runtime.storage.list_events_for_task(task_id)
        event_types = [event.event_type for event in events]
        self.assertIn("skill.forced_selected", event_types)
        selected = next(event for event in events if event.event_type == "skill.forced_selected")
        self.assertTrue(selected.payload.get("skill_bundle_revision"))
        plan_built = next(event for event in events if event.event_type == "workflow.plan_built")
        self.assertTrue(plan_built.payload["metadata"].get("skill_bundle_revision"))

        after = await self.client.get("/api/v1/capabilities")
        self.assertIn("skill.demo_hot_reload", {item["capability_id"] for item in after.json()["capabilities"]})
        audit_records = [
            json.loads(line)
            for line in (self.workspace / "audit.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertTrue(any(record["event_type"] == "skill.bundle_refresh_completed" for record in audit_records))
        self.assertTrue(
            any(
                record["event_type"] == "skill.capability_registered"
                and record["payload"]["capability_id"] == "skill.demo_hot_reload"
                for record in audit_records
            )
        )

    async def test_explicit_new_skill_route_refreshes_before_capability_validation(self) -> None:
        project_skill_root = self.workspace / "skill"
        project_skill_root.mkdir(parents=True)

        async def streamer(_prompt: str, **_kwargs):
            yield "done"

        await self.reconfigure_runtime(
            skill_roots=(project_skill_root,),
            public_skill_roots=(project_skill_root,),
            main_agent_stream_generator=streamer,
        )
        self._write_skill(project_skill_root)

        response = await self.submit_message(
            conversation_id="conv-explicit-hot",
            content="请处理动态加载任务",
            capability_id="skill.demo_hot_reload",
        )
        self.assertEqual(response.status_code, 202)
        terminal = await self.wait_for_terminal_task(response.json()["task_id"])
        self.assertEqual(terminal["status"], "completed")
        events = await self.runtime.storage.list_events_for_task(response.json()["task_id"])
        decision = next(event for event in events if event.event_type == "soft_skill_binding.decision")
        self.assertEqual(decision.payload["decision"], "execute")
        self.assertEqual(decision.payload["target_capability_id"], "skill.demo_hot_reload")

    async def test_new_conversation_refresh_removes_deleted_skill_from_capabilities_and_prompt(self) -> None:
        project_skill_root = self.workspace / "skill"
        self._write_skill(project_skill_root)
        prompts: list[str] = []

        def planner(prompt: str, **_kwargs) -> str:
            prompts.append(prompt)
            return json.dumps({"nodes": [{"node_id": "answer", "capability_id": "main_agent.respond"}]})

        async def streamer(_prompt: str, **_kwargs):
            yield "done"

        await self.reconfigure_runtime(
            skill_roots=(project_skill_root,),
            public_skill_roots=(project_skill_root,),
            planner_text_generator=planner,
            main_agent_stream_generator=streamer,
        )
        before = await self.client.get("/api/v1/capabilities")
        self.assertIn("skill.demo_hot_reload", {item["capability_id"] for item in before.json()["capabilities"]})

        (project_skill_root / "demo-hot-reload" / "SKILL.md").unlink()
        response = await self.submit_message(conversation_id="conv-delete", content="普通问候", capability_id=None)
        self.assertEqual(response.status_code, 202)
        terminal = await self.wait_for_terminal_task(response.json()["task_id"])
        self.assertEqual(terminal["status"], "completed")

        self.assertNotIn("skill.demo_hot_reload", prompts[0])
        after = await self.client.get("/api/v1/capabilities")
        self.assertNotIn("skill.demo_hot_reload", {item["capability_id"] for item in after.json()["capabilities"]})

    async def test_upload_created_conversation_without_tasks_still_refreshes_before_first_message(self) -> None:
        project_skill_root = self.workspace / "skill"
        project_skill_root.mkdir(parents=True)
        prompts: list[str] = []

        def planner(prompt: str, **_kwargs) -> str:
            prompts.append(prompt)
            return json.dumps({"nodes": [{"node_id": "demo", "capability_id": "skill.demo_hot_reload"}]})

        async def streamer(_prompt: str, **_kwargs):
            yield "done"

        await self.reconfigure_runtime(
            skill_roots=(project_skill_root,),
            public_skill_roots=(project_skill_root,),
            planner_text_generator=planner,
            main_agent_stream_generator=streamer,
        )
        upload = await self.client.post(
            "/api/v1/conversations/uploads",
            data={"conversation_id": "conv-upload-first"},
            files={"file": ("input.csv", b"name\nhello\n", "text/csv")},
        )
        self.assertEqual(upload.status_code, 201)

        self._write_skill(project_skill_root)
        response = await self.submit_message(conversation_id="conv-upload-first", content="请处理动态加载任务", capability_id=None)
        self.assertEqual(response.status_code, 202)
        terminal = await self.wait_for_terminal_task(response.json()["task_id"])
        self.assertEqual(terminal["status"], "completed")
        self.assertIn("skill.demo_hot_reload", prompts[0])

    async def test_refresh_sync_failure_restores_previous_active_skill_bundle(self) -> None:
        project_skill_root = self.workspace / "skill"
        self._write_skill(project_skill_root, name="baseline-skill", description="基础 Skill")

        async def streamer(_prompt: str, **_kwargs):
            yield "done"

        await self.reconfigure_runtime(
            skill_roots=(project_skill_root,),
            public_skill_roots=(project_skill_root,),
            main_agent_stream_generator=streamer,
        )
        previous_revision = self.runtime._skill_runtime_state.active_revision  # noqa: SLF001 - test validates runtime rollback seam
        before = await self.client.get("/api/v1/capabilities")
        before_ids = {item["capability_id"] for item in before.json()["capabilities"]}

        self._write_skill(project_skill_root, name="new-skill", description="新增 Skill")
        with patch.object(self.runtime, "_sync_skill_capability_registry", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                await self.submit_message(
                    conversation_id="conv-refresh-failure",
                    content="触发刷新失败",
                    capability_id=None,
                )

        self.assertEqual(self.runtime._skill_runtime_state.active_revision, previous_revision)  # noqa: SLF001
        after = await self.client.get("/api/v1/capabilities")
        after_ids = {item["capability_id"] for item in after.json()["capabilities"]}
        self.assertEqual(before_ids, after_ids)
