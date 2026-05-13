from __future__ import annotations

import asyncio
import tempfile
import textwrap
import unittest
from pathlib import Path

from src.capabilities.skill_tool import SkillExecutor
from src.core.contracts import CapabilityExecutionRequest
from src.integrations.codex_skills import SkillRuntimeState, SkillScriptRunner
from src.integrations.codex_skills.execution import SkillPlatformHandlerRegistry, SkillServiceRegistry


class SkillExecutorTest(unittest.IsolatedAsyncioTestCase):
    def _build_state(self, root: Path) -> SkillRuntimeState:
        return SkillRuntimeState.from_roots(
            skill_roots=(root,),
            public_skill_roots=(root,),
            reserved_capability_ids=('main_agent.respond',),
        )

    async def test_executes_python_subprocess_skill(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / 'skill'
            skill_dir = root / 'echo'
            scripts = skill_dir / 'scripts'
            scripts.mkdir(parents=True)
            (scripts / 'echo.py').write_text(
                textwrap.dedent(
                    '''
                    import json, sys
                    payload = json.load(sys.stdin)
                    print(json.dumps({"summary": "processed " + payload["query"]}, ensure_ascii=False))
                    '''
                ).strip(),
                encoding='utf-8',
            )
            (skill_dir / 'SKILL.md').write_text(
                """---
name: echo
description: 回显处理
scripts:
  - name: echo
    path: scripts/echo.py
    runtime: python
outputs:
  required:
    - summary
---

# Echo
执行脚本。
""",
                encoding='utf-8',
            )
            state = self._build_state(root)
            executor = SkillExecutor(runtime_state=state, script_runner=SkillScriptRunner())
            result = await executor.execute(
                CapabilityExecutionRequest(
                    capability_id='skill.echo',
                    conversation_id='conv-1',
                    task_id='task-1',
                    node_id='node-1',
                    input_payload={'user_message': 'hello'},
                    metadata={'skill_bundle_revision': state.active_revision},
                )
            )

        self.assertIsNone(result.error)
        self.assertEqual(result.output_payload['summary'], 'processed hello')
        self.assertIn('skill.execution_completed', [event.event_type for event in result.events])

    async def test_missing_required_input_returns_controlled_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / 'skill'
            skill_dir = root / 'need-variety'
            scripts = skill_dir / 'scripts'
            scripts.mkdir(parents=True)
            (scripts / 'echo.py').write_text('import json, sys\nprint(json.dumps({"summary": "ok"}, ensure_ascii=False))', encoding='utf-8')
            (skill_dir / 'SKILL.md').write_text(
                """---
name: need-variety
description: 需要品种
scripts:
  - name: echo
    path: scripts/echo.py
    runtime: python
    inputs:
      required:
        - variety
outputs:
  required:
    - summary
---

# Need Variety
执行脚本。
""",
                encoding='utf-8',
            )
            state = self._build_state(root)
            executor = SkillExecutor(runtime_state=state, script_runner=SkillScriptRunner())
            result = await executor.execute(
                CapabilityExecutionRequest(
                    capability_id='skill.need_variety',
                    conversation_id='conv-1',
                    task_id='task-1',
                    node_id='node-1',
                    input_payload={'user_message': 'hello'},
                    metadata={'skill_bundle_revision': state.active_revision},
                )
            )

        self.assertIsNotNone(result.error)
        self.assertEqual(result.error.code, 'skill_input_missing')

    async def test_platform_service_rejects_unregistered_handler(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / 'skill'
            skill_dir = root / 'platform'
            skill_dir.mkdir(parents=True)
            (skill_dir / 'SKILL.md').write_text(
                """---
name: platform
description: 平台服务
execution:
  mode: platform_service
  answer_mode: direct
  trust_scope: project
  handler: demo.handler
  services:
    - demo.service
---

# Platform
平台服务。
""",
                encoding='utf-8',
            )
            state = self._build_state(root)
            executor = SkillExecutor(
                runtime_state=state,
                platform_handler_registry=SkillPlatformHandlerRegistry(),
                service_registry=SkillServiceRegistry(),
            )
            result = await executor.execute(
                CapabilityExecutionRequest(
                    capability_id='skill.platform',
                    conversation_id='conv-1',
                    task_id='task-1',
                    node_id='node-1',
                    input_payload={'user_message': 'hello'},
                    metadata={'skill_bundle_revision': state.active_revision},
                )
            )

        self.assertIsNotNone(result.error)
        self.assertEqual(result.error.code, 'skill_service_denied')

    async def test_platform_service_rejects_registered_handler_without_skill_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / 'skill'
            skill_dir = root / 'platform'
            skill_dir.mkdir(parents=True)
            (skill_dir / 'SKILL.md').write_text(
                """---
name: platform
description: 平台服务
execution:
  mode: platform_service
  answer_mode: direct
  handler: demo.handler
---

# Platform
平台服务。
""",
                encoding='utf-8',
            )
            state = self._build_state(root)
            handlers = SkillPlatformHandlerRegistry(
                handlers={'demo.handler': lambda ctx: {'response_text': 'should not run'}},
            )
            executor = SkillExecutor(
                runtime_state=state,
                platform_handler_registry=handlers,
            )
            result = await executor.execute(
                CapabilityExecutionRequest(
                    capability_id='skill.platform',
                    conversation_id='conv-1',
                    task_id='task-1',
                    node_id='node-1',
                    input_payload={'user_message': 'hello'},
                    metadata={'skill_bundle_revision': state.active_revision},
                )
            )

        self.assertIsNotNone(result.error)
        self.assertEqual(result.error.code, 'skill_service_denied')

    async def test_unknown_skill_bundle_revision_returns_specific_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / 'skill'
            skill_dir = root / 'echo'
            scripts = skill_dir / 'scripts'
            scripts.mkdir(parents=True)
            (scripts / 'echo.py').write_text(
                'import json, sys\nprint(json.dumps({"summary": "ok"}, ensure_ascii=False))',
                encoding='utf-8',
            )
            (skill_dir / 'SKILL.md').write_text(
                """---
name: echo
description: 回显处理
scripts:
  - name: echo
    path: scripts/echo.py
    runtime: python
outputs:
  required:
    - summary
---

# Echo
执行脚本。
""",
                encoding='utf-8',
            )
            state = self._build_state(root)
            executor = SkillExecutor(runtime_state=state, script_runner=SkillScriptRunner())
            result = await executor.execute(
                CapabilityExecutionRequest(
                    capability_id='skill.echo',
                    conversation_id='conv-1',
                    task_id='task-1',
                    node_id='node-1',
                    input_payload={'user_message': 'hello'},
                    metadata={'skill_bundle_revision': 'missing-revision'},
                )
            )

        self.assertIsNotNone(result.error)
        self.assertEqual(result.error.code, 'skill_bundle_revision_missing')

    async def test_script_timeout_maps_to_specific_error_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / 'skill'
            skill_dir = root / 'slow'
            scripts = skill_dir / 'scripts'
            scripts.mkdir(parents=True)
            (scripts / 'slow.py').write_text(
                textwrap.dedent(
                    '''
                    import json, sys, time
                    payload = json.load(sys.stdin)
                    time.sleep(0.2)
                    print(json.dumps({"summary": payload["query"]}, ensure_ascii=False))
                    '''
                ).strip(),
                encoding='utf-8',
            )
            (skill_dir / 'SKILL.md').write_text(
                """---
name: slow
description: 超时脚本
scripts:
  - name: slow
    path: scripts/slow.py
    runtime: python
    timeout_seconds: 0.01
outputs:
  required:
    - summary
---

# Slow
执行脚本。
""",
                encoding='utf-8',
            )
            state = self._build_state(root)
            executor = SkillExecutor(runtime_state=state, script_runner=SkillScriptRunner())
            result = await executor.execute(
                CapabilityExecutionRequest(
                    capability_id='skill.slow',
                    conversation_id='conv-1',
                    task_id='task-1',
                    node_id='node-1',
                    input_payload={'user_message': 'hello'},
                    metadata={'skill_bundle_revision': state.active_revision},
                )
            )

        self.assertIsNotNone(result.error)
        self.assertEqual(result.error.code, 'skill_script_timeout')
        self.assertTrue(result.error.retriable)

    async def test_invalid_script_output_maps_to_output_validation_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / 'skill'
            skill_dir = root / 'invalid-output'
            scripts = skill_dir / 'scripts'
            scripts.mkdir(parents=True)
            (scripts / 'bad.py').write_text(
                'import json, sys\npayload = json.load(sys.stdin)\nprint(json.dumps({"wrong": payload["query"]}, ensure_ascii=False))',
                encoding='utf-8',
            )
            (skill_dir / 'SKILL.md').write_text(
                """---
name: invalid-output
description: 非法输出
scripts:
  - name: bad
    path: scripts/bad.py
    runtime: python
outputs:
  required:
    - summary
---

# Invalid Output
执行脚本。
""",
                encoding='utf-8',
            )
            state = self._build_state(root)
            executor = SkillExecutor(runtime_state=state, script_runner=SkillScriptRunner())
            result = await executor.execute(
                CapabilityExecutionRequest(
                    capability_id='skill.invalid_output',
                    conversation_id='conv-1',
                    task_id='task-1',
                    node_id='node-1',
                    input_payload={'user_message': 'hello'},
                    metadata={'skill_bundle_revision': state.active_revision},
                )
            )

        self.assertIsNotNone(result.error)
        self.assertEqual(result.error.code, 'skill_output_validation_failed')

    async def test_platform_service_uses_registered_handler_and_returns_text_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / 'skill'
            skill_dir = root / 'platform'
            skill_dir.mkdir(parents=True)
            (skill_dir / 'SKILL.md').write_text(
                """---
name: platform
description: 平台服务
execution:
  mode: platform_service
  answer_mode: direct
  trust_scope: project
  handler: demo.handler
  services:
    - demo.service
---

# Platform
平台服务。
""",
                encoding='utf-8',
            )
            state = self._build_state(root)
            services = SkillServiceRegistry({'demo.service': object()})
            handlers = SkillPlatformHandlerRegistry(
                handlers={'demo.handler': lambda ctx: {'response_text': 'handled ' + ctx.input_payload['query']}},
                trusted_skill_handlers={'skill.platform': 'demo.handler'},
                trusted_skill_services={'skill.platform': ('demo.service',)},
            )
            executor = SkillExecutor(
                runtime_state=state,
                platform_handler_registry=handlers,
                service_registry=services,
            )
            result = await executor.execute(
                CapabilityExecutionRequest(
                    capability_id='skill.platform',
                    conversation_id='conv-1',
                    task_id='task-1',
                    node_id='node-1',
                    input_payload={'user_message': 'hello'},
                    metadata={'skill_bundle_revision': state.active_revision},
                )
            )

        self.assertIsNone(result.error)
        self.assertEqual(result.output_payload['response_text'], 'handled hello')
        self.assertEqual(result.artifacts[0].artifact_type.value, 'text')

    async def test_project_platform_handler_loads_from_public_skill_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / 'skill'
            skill_dir = root / 'platform'
            runtime_dir = skill_dir / 'runtime'
            runtime_dir.mkdir(parents=True)
            (runtime_dir / 'platform_handler.py').write_text(
                textwrap.dedent(
                    '''
                    def build_handler():
                        def handle(context):
                            marker = context.services["demo.service"]
                            return {"response_text": f"project {marker} {context.input_payload['query']}"}
                        return handle
                    '''
                ).strip(),
                encoding='utf-8',
            )
            (skill_dir / 'SKILL.md').write_text(
                """---
name: platform
description: 平台服务
execution:
  mode: platform_service
  answer_mode: direct
  trust_scope: project
  handler: skill.platform.handler
  handler_module: runtime/platform_handler.py
  services:
    - demo.service
---

# Platform
平台服务。
""",
                encoding='utf-8',
            )
            state = self._build_state(root)
            executor = SkillExecutor(
                runtime_state=state,
                service_registry=SkillServiceRegistry({'demo.service': 'svc'}),
            )
            result = await executor.execute(
                CapabilityExecutionRequest(
                    capability_id='skill.platform',
                    conversation_id='conv-1',
                    task_id='task-1',
                    node_id='node-1',
                    input_payload={'user_message': 'hello'},
                    metadata={'skill_bundle_revision': state.active_revision},
                )
            )

        self.assertIsNone(result.error)
        self.assertEqual(result.output_payload['response_text'], 'project svc hello')

    async def test_project_platform_handler_rejects_outside_module_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / 'skill'
            skill_dir = root / 'platform'
            skill_dir.mkdir(parents=True)
            outside = Path(tmpdir) / 'outside_handler.py'
            outside.write_text('def build_handler():\n    return lambda context: {"response_text": "bad"}\n', encoding='utf-8')
            (skill_dir / 'SKILL.md').write_text(
                f"""---
name: platform
description: 平台服务
execution:
  mode: platform_service
  answer_mode: direct
  trust_scope: project
  handler: skill.platform.handler
  handler_module: {outside.as_posix()}
---

# Platform
平台服务。
""",
                encoding='utf-8',
            )
            state = self._build_state(root)
            executor = SkillExecutor(runtime_state=state)
            result = await executor.execute(
                CapabilityExecutionRequest(
                    capability_id='skill.platform',
                    conversation_id='conv-1',
                    task_id='task-1',
                    node_id='node-1',
                    input_payload={'user_message': 'hello'},
                    metadata={'skill_bundle_revision': state.active_revision},
                )
            )

        self.assertIsNotNone(result.error)
        self.assertEqual(result.error.code, 'skill_service_denied')

    async def test_project_platform_handler_services_fail_closed_when_unregistered(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / 'skill'
            skill_dir = root / 'platform'
            runtime_dir = skill_dir / 'runtime'
            runtime_dir.mkdir(parents=True)
            (runtime_dir / 'platform_handler.py').write_text(
                'def build_handler():\n    return lambda context: {"response_text": "should not run"}\n',
                encoding='utf-8',
            )
            (skill_dir / 'SKILL.md').write_text(
                """---
name: platform
description: 平台服务
execution:
  mode: platform_service
  answer_mode: direct
  trust_scope: project
  handler: skill.platform.handler
  handler_module: runtime/platform_handler.py
  services:
    - missing.service
---

# Platform
平台服务。
""",
                encoding='utf-8',
            )
            state = self._build_state(root)
            executor = SkillExecutor(runtime_state=state, service_registry=SkillServiceRegistry())
            result = await executor.execute(
                CapabilityExecutionRequest(
                    capability_id='skill.platform',
                    conversation_id='conv-1',
                    task_id='task-1',
                    node_id='node-1',
                    input_payload={'user_message': 'hello'},
                    metadata={'skill_bundle_revision': state.active_revision},
                )
            )

        self.assertIsNotNone(result.error)
        self.assertEqual(result.error.code, 'skill_service_denied')

    async def test_platform_service_rejects_missing_runtime_service_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / 'skill'
            skill_dir = root / 'platform'
            skill_dir.mkdir(parents=True)
            (skill_dir / 'SKILL.md').write_text(
                """---
name: platform
description: 平台服务
execution:
  mode: platform_service
  answer_mode: direct
  trust_scope: project
  handler: demo.handler
  services:
    - demo.service
---

# Platform
平台服务。
""",
                encoding='utf-8',
            )
            state = self._build_state(root)
            handlers = SkillPlatformHandlerRegistry(
                handlers={'demo.handler': lambda ctx: {'response_text': 'handled ' + ctx.input_payload['query']}},
                trusted_skill_handlers={'skill.platform': 'demo.handler'},
                trusted_skill_services={'skill.platform': ('demo.service',)},
            )
            executor = SkillExecutor(
                runtime_state=state,
                platform_handler_registry=handlers,
                service_registry=SkillServiceRegistry(),
            )
            result = await executor.execute(
                CapabilityExecutionRequest(
                    capability_id='skill.platform',
                    conversation_id='conv-1',
                    task_id='task-1',
                    node_id='node-1',
                    input_payload={'user_message': 'hello'},
                    metadata={'skill_bundle_revision': state.active_revision},
                )
            )

        self.assertIsNotNone(result.error)
        self.assertEqual(result.error.code, 'skill_service_denied')

    async def test_platform_service_error_is_not_audited_as_completed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / 'skill'
            skill_dir = root / 'platform'
            skill_dir.mkdir(parents=True)
            (skill_dir / 'SKILL.md').write_text(
                """---
name: platform
description: 平台服务
execution:
  mode: platform_service
  answer_mode: direct
  trust_scope: project
  handler: demo.handler
---

# Platform
平台服务。
""",
                encoding='utf-8',
            )
            state = self._build_state(root)

            async def erroring_handler(_ctx):
                from src.core.contracts import CapabilityExecutionError
                from src.integrations.codex_skills.execution import SkillPlatformHandlerResult

                return SkillPlatformHandlerResult(
                    output_payload={'response_text': 'handled'},
                    error=CapabilityExecutionError(code='handler_failed', message='boom', retriable=False),
                )

            handlers = SkillPlatformHandlerRegistry(
                handlers={'demo.handler': erroring_handler},
                trusted_skill_handlers={'skill.platform': 'demo.handler'},
            )
            executor = SkillExecutor(
                runtime_state=state,
                platform_handler_registry=handlers,
                service_registry=SkillServiceRegistry(),
            )
            result = await executor.execute(
                CapabilityExecutionRequest(
                    capability_id='skill.platform',
                    conversation_id='conv-1',
                    task_id='task-1',
                    node_id='node-1',
                    input_payload={'user_message': 'hello'},
                    metadata={'skill_bundle_revision': state.active_revision},
                )
            )

        self.assertIsNotNone(result.error)
        event_types = [event.event_type for event in result.events]
        self.assertIn('skill.execution_failed', event_types)
        self.assertNotIn('skill.execution_completed', event_types)
