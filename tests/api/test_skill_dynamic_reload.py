from __future__ import annotations

import json
from unittest.mock import patch

from src.integrations.agent_skills.skill_runtime_state import SkillRuntimeRefreshResult
from src.orchestration.agent_loop.models import AgentItemKind
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

        def agent_generator(prompt: str, **_kwargs) -> str:
            prompts.append(prompt)
            if '"schema":"maf.agent.delegated_skill_activation.v1"' in prompt:
                return "done"
            return json.dumps(
                {
                    "tool_calls": [
                        {
                            "capability_id": "skill.demo_hot_reload",
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
        before = await self.client.get("/api/v1/capabilities")
        self.assertNotIn("skill.demo_hot_reload", {item["capability_id"] for item in before.json()["capabilities"]})

        self._write_skill(project_skill_root)
        response = await self.submit_message(conversation_id="conv-hot", content="请处理动态加载任务", capability_id=None)
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]
        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")

        self.assertGreaterEqual(len(prompts), 2)
        nodes = await self.runtime.storage.list_task_nodes_for_task(task_id)
        self.assertEqual(
            [node.capability_id for node in nodes],
            ["skill.demo_hot_reload", "agent.final_output"],
        )
        run = await self.runtime.agent_run_repository.get_run_for_task(task_id)
        assert run is not None
        items = await self.runtime.agent_run_repository.list_items(run.run_id)
        activation = next(
            item for item in items if item.kind is AgentItemKind.SKILL_ACTIVATION
        )
        self.assertIn(
            self.runtime._skill_runtime_state.active_revision,
            activation.payload_json,
        )

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
        self.assertNotIn(
            "skill.question_answered",
            {event.event_type for event in events},
        )
        run = await self.runtime.agent_run_repository.get_run_for_task(
            response.json()["task_id"]
        )
        assert run is not None
        items = await self.runtime.agent_run_repository.list_items(run.run_id)
        self.assertEqual(
            sum(item.kind is AgentItemKind.SKILL_ACTIVATION for item in items),
            1,
        )

    async def test_new_conversation_refresh_removes_deleted_skill_from_capabilities_and_prompt(self) -> None:
        project_skill_root = self.workspace / "skill"
        self._write_skill(project_skill_root)
        prompts: list[str] = []

        def agent_generator(prompt: str, **_kwargs) -> str:
            prompts.append(prompt)
            return "done"

        await self.reconfigure_runtime(
            skill_roots=(project_skill_root,),
            public_skill_roots=(project_skill_root,),
            main_agent_stream_generator=agent_generator,
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

        def agent_generator(prompt: str, **_kwargs) -> str:
            prompts.append(prompt)
            if '"schema":"maf.agent.delegated_skill_activation.v1"' in prompt:
                return "done"
            return json.dumps(
                {
                    "tool_calls": [
                        {
                            "capability_id": "skill.demo_hot_reload",
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
        self.assertGreaterEqual(len(prompts), 2)

    async def test_hint_delegated_skill_reuses_activation_and_reveals_pinned_body_after_call(self) -> None:
        project_skill_root = self.workspace / "skill"
        self._write_skill(project_skill_root)
        prompts: list[str] = []

        def agent_generator(prompt: str, **_kwargs) -> str:
            prompts.append(prompt)
            if '"schema":"maf.agent.delegated_skill_activation.v1"' in prompt:
                return "done"
            return json.dumps(
                {
                    "tool_calls": [
                        {
                            "capability_id": "skill.demo_hot_reload",
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
        response = await self.client.post(
            "/api/v1/conversations/chat-messages",
            json={
                "conversation_id": "conv-delegated-hint",
                "content": "please execute this skill",
                "routing_mode": "hint",
                "capability_id": "skill.demo_hot_reload",
                "metadata": {},
            },
        )
        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]
        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")
        self.assertNotIn("请使用动态加载 Skill。", prompts[0])
        self.assertIn("请使用动态加载 Skill。", prompts[1])

        run = await self.runtime.agent_run_repository.get_run_for_task(task_id)
        assert run is not None
        items = await self.runtime.agent_run_repository.list_items(run.run_id)
        activations = [
            item for item in items if item.kind is AgentItemKind.SKILL_ACTIVATION
        ]
        self.assertEqual(len(activations), 1)
        self.assertEqual(json.loads(activations[0].payload_json)["binding_mode"], "hint")
        tool_result = next(
            item for item in items if item.kind is AgentItemKind.TOOL_RESULT
        )
        safe_result = json.loads(tool_result.payload_json)["safe_result"]
        self.assertEqual(safe_result["schema"], "maf.agent.model_result.v1")
        self.assertEqual(
            safe_result["projection_revision"],
            "delegated-skill-instruction-v1",
        )
        self.assertEqual(
            safe_result["model_view"]["schema"],
            "maf.agent.delegated_skill_activation.v1",
        )

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
        after_ids = {descriptor.capability_id for descriptor in self.runtime.capability_registry.list(public_only=True)}
        self.assertEqual(before_ids, after_ids)

    async def test_new_conversation_refresh_failed_result_raises_before_submit_message(self) -> None:
        project_skill_root = self.workspace / "skill"
        self._write_skill(project_skill_root, name="baseline-skill", description="基础 Skill")
        await self.reconfigure_runtime(skill_roots=(project_skill_root,), public_skill_roots=(project_skill_root,))
        previous_revision = self.runtime._skill_runtime_state.active_revision  # noqa: SLF001 - test validates refresh seam

        failed_result = SkillRuntimeRefreshResult(
            status="failed",
            reason="conversation_start",
            previous_revision=previous_revision,
            active_revision=previous_revision,
            registered_count=1,
            skipped_count=0,
            duration_ms=1,
            script_package_snapshot=False,
            error_type="RuntimeError",
        )
        with patch.object(self.runtime._skill_runtime_state, "refresh_if_changed", return_value=failed_result):  # noqa: SLF001
            with self.assertRaises(RuntimeError):
                await self.submit_message(
                    conversation_id="conv-refresh-failed-result",
                    content="触发刷新返回失败",
                    capability_id=None,
                )

        audit_records = [
            json.loads(line)
            for line in (self.workspace / "audit.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        failed = [record for record in audit_records if record["event_type"] == "skill.bundle_refresh_failed"]
        self.assertTrue(failed)
        self.assertEqual(failed[-1]["payload"]["reason"], "conversation_start")
        self.assertEqual(failed[-1]["payload"]["fallback_revision"], previous_revision)
