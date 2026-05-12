from __future__ import annotations

from tests.api.support import APITestCase


class SkillExecutorRuntimeAPITest(APITestCase):
    async def test_explicit_python_subprocess_skill_executes_direct_answer(self) -> None:
        project_skill_root = self.workspace / 'skill'
        skill_dir = project_skill_root / 'echo'
        scripts_dir = skill_dir / 'scripts'
        scripts_dir.mkdir(parents=True)
        (scripts_dir / 'echo.py').write_text(
            'import json, sys\npayload = json.load(sys.stdin)\nprint(json.dumps({"response_text": "echo: " + payload["query"]}, ensure_ascii=False))',
            encoding='utf-8',
        )
        (skill_dir / 'SKILL.md').write_text(
            """---
name: echo
description: 直接回显
scripts:
  - name: echo
    path: scripts/echo.py
    runtime: python
execution:
  answer_mode: direct
outputs:
  required:
    - response_text
---

# Echo
执行脚本并直接回答。
""",
            encoding='utf-8',
        )

        await self.reconfigure_runtime(
            skill_roots=(project_skill_root,),
            public_skill_roots=(project_skill_root,),
        )

        response = await self.submit_message(
            conversation_id='conv-skill-direct',
            content='hello skill',
            capability_id='skill.echo',
        )
        self.assertEqual(response.status_code, 202)
        task_id = response.json()['task_id']
        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal['status'], 'completed')

        nodes = await self.runtime.storage.list_task_nodes_for_task(task_id)
        self.assertEqual([node.capability_id for node in nodes], ['skill.echo'])
        artifacts = await self.runtime.storage.list_artifacts_for_task(task_id)
        self.assertEqual([str(artifact.artifact_type) for artifact in artifacts], ['text'])
        self.assertEqual(artifacts[0].storage_ref, 'echo: hello skill')

    async def test_new_conversation_refreshes_executor_mode_skill_and_syncs_instance_support(self) -> None:
        project_skill_root = self.workspace / 'skill'
        project_skill_root.mkdir(parents=True)
        await self.reconfigure_runtime(
            skill_roots=(project_skill_root,),
            public_skill_roots=(project_skill_root,),
            main_agent_stream_generator=lambda _prompt, **_kwargs: _single_chunk('finalized'),
        )

        skill_dir = project_skill_root / 'executor-demo'
        scripts_dir = skill_dir / 'scripts'
        scripts_dir.mkdir(parents=True)
        (scripts_dir / 'echo.py').write_text(
            'import json, sys\npayload = json.load(sys.stdin)\nprint(json.dumps({"summary": "processed " + payload["query"]}, ensure_ascii=False))',
            encoding='utf-8',
        )
        (skill_dir / 'SKILL.md').write_text(
            """---
name: executor-demo
description: 需要 finalizer 的技能
scripts:
  - name: echo
    path: scripts/echo.py
    runtime: python
outputs:
  required:
    - summary
---

# Executor Demo
运行脚本。
""",
            encoding='utf-8',
        )

        response = await self.submit_message(
            conversation_id='conv-executor-hot',
            content='refresh skill',
            capability_id='skill.executor_demo',
        )
        self.assertEqual(response.status_code, 202)
        task_id = response.json()['task_id']
        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal['status'], 'completed')

        nodes = await self.runtime.storage.list_task_nodes_for_task(task_id)
        self.assertEqual({node.capability_id for node in nodes}, {'skill.executor_demo', 'main_agent.respond'})
        instance = next(item for item in self.runtime.instance_registry.list() if item.instance_id == 'inst-skill-local')
        self.assertIn('skill.executor_demo', instance.supported_capabilities)


async def _single_chunk(text: str, **_kwargs):
    yield text
