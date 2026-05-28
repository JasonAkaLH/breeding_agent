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
        self.assertEqual(result.output_payload['response_text'], 'processed hello')
        self.assertIn('skill.execution_completed', [event.event_type for event in result.events])

    async def test_python_subprocess_requires_finalizer_normalizes_answer_without_text_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / 'skill'
            skill_dir = root / 'rcbd'
            scripts = skill_dir / 'scripts'
            scripts.mkdir(parents=True)
            (scripts / 'answer.py').write_text(
                'import json, sys\njson.load(sys.stdin)\nprint(json.dumps({"answer": "RCBD 设计已完成"}, ensure_ascii=False))',
                encoding='utf-8',
            )
            (skill_dir / 'SKILL.md').write_text(
                """---
name: rcbd
description: 生成随机区组设计
scripts:
  - name: answer
    path: scripts/answer.py
    runtime: python
outputs:
  required:
    - answer
---

# RCBD
执行脚本。
""",
                encoding='utf-8',
            )
            state = self._build_state(root)
            executor = SkillExecutor(runtime_state=state, script_runner=SkillScriptRunner())
            result = await executor.execute(
                CapabilityExecutionRequest(
                    capability_id='skill.rcbd',
                    conversation_id='conv-1',
                    task_id='task-1',
                    node_id='node-1',
                    input_payload={'user_message': '做 3 次重复 RCBD'},
                    metadata={'skill_bundle_revision': state.active_revision},
                )
            )

        self.assertIsNone(result.error)
        self.assertEqual(result.output_payload['answer'], 'RCBD 设计已完成')
        self.assertEqual(result.output_payload['response_text'], 'RCBD 设计已完成')
        self.assertEqual(result.artifacts, ())

    async def test_python_subprocess_receives_raw_skill_artifact_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / 'skill'
            skill_dir = root / 'materials'
            scripts = skill_dir / 'scripts'
            scripts.mkdir(parents=True)
            (scripts / 'read_materials.py').write_text(
                textwrap.dedent(
                    '''
                    import json, sys
                    payload = json.load(sys.stdin)
                    artifacts = payload.get("uploaded_artifacts") or []
                    content = artifacts[0].get("content", "") if artifacts else ""
                    print(json.dumps(
                        {
                            "answer": "content-bytes:%d" % len(content.encode("utf-8")),
                            "raw_content_seen": bool(content),
                        },
                        ensure_ascii=False,
                    ))
                    '''
                ).strip(),
                encoding='utf-8',
            )
            (skill_dir / 'SKILL.md').write_text(
                """---
name: materials
description: 读取上传材料
scripts:
  - name: read_materials
    path: scripts/read_materials.py
    runtime: python
outputs:
  required:
    - answer
---

# Materials
执行脚本。
""",
                encoding='utf-8',
            )
            state = self._build_state(root)
            executor = SkillExecutor(runtime_state=state, script_runner=SkillScriptRunner())
            raw_content = 'plot_id,hyb_check,set\n1,A,A\n'
            result = await executor.execute(
                CapabilityExecutionRequest(
                    capability_id='skill.materials',
                    conversation_id='conv-1',
                    task_id='task-1',
                    node_id='node-1',
                    input_payload={'user_message': '读取材料'},
                    metadata={
                        'skill_bundle_revision': state.active_revision,
                        'uploaded_artifacts': [
                            {
                                'upload_id': 'upl-1',
                                'filename': 'materials.csv',
                                'preview': {'row_count': 1},
                            }
                        ],
                        'skill_artifacts': [
                            {
                                'upload_id': 'upl-1',
                                'filename': 'materials.csv',
                                'preview': {'row_count': 1},
                                'content': raw_content,
                            }
                        ],
                    },
                )
            )

        self.assertIsNone(result.error)
        self.assertEqual(result.output_payload['answer'], f'content-bytes:{len(raw_content.encode("utf-8"))}')
        self.assertEqual(result.output_payload['response_text'], f'content-bytes:{len(raw_content.encode("utf-8"))}')
        self.assertIs(result.output_payload['raw_content_seen'], True)

    async def test_python_subprocess_slot_llm_does_not_receive_raw_skill_artifact_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / 'skill'
            skill_dir = root / 'material-slot'
            scripts = skill_dir / 'scripts'
            scripts.mkdir(parents=True)
            (scripts / 'read_materials.py').write_text(
                textwrap.dedent(
                    '''
                    import json, sys
                    payload = json.load(sys.stdin)
                    artifacts = payload.get("uploaded_artifacts") or []
                    content = artifacts[0].get("content", "") if artifacts else ""
                    print(json.dumps(
                        {
                            "answer": "%s:%d" % (payload.get("variety"), len(content.encode("utf-8"))),
                            "raw_content_seen": bool(content),
                        },
                        ensure_ascii=False,
                    ))
                    '''
                ).strip(),
                encoding='utf-8',
            )
            (skill_dir / 'SKILL.md').write_text(
                """---
name: material-slot
description: 读取上传材料并补槽
scripts:
  - name: read_materials
    path: scripts/read_materials.py
    runtime: python
outputs:
  required:
    - answer
parameters:
  material_data:
    type: artifact
    required: true
    source: artifact
  variety:
    type: string
    required: true
---

# Material Slot
执行脚本。
""",
                encoding='utf-8',
            )
            state = self._build_state(root)
            prompts: list[str] = []
            raw_content = 'plot_id,hyb_check,set\n1,A,A\n'

            def slot_generator(prompt: str):
                prompts.append(prompt)
                self.assertNotIn(raw_content, prompt)
                self.assertNotIn('1,A,A', prompt)
                return '{"resolved":{"variety":{"value":"龙粳33","source":"query"}},"missing":[]}'

            executor = SkillExecutor(
                runtime_state=state,
                script_runner=SkillScriptRunner(),
                skill_input_text_generator=slot_generator,
            )
            result = await executor.execute(
                CapabilityExecutionRequest(
                    capability_id='skill.material_slot',
                    conversation_id='conv-1',
                    task_id='task-1',
                    node_id='node-1',
                    input_payload={'user_message': '请处理这个材料文件'},
                    metadata={
                        'skill_bundle_revision': state.active_revision,
                        'uploaded_artifacts': [
                            {
                                'upload_id': 'upl-1',
                                'filename': 'materials.csv',
                                'preview': {'row_count': 1},
                            }
                        ],
                        'skill_artifacts': [
                            {
                                'upload_id': 'upl-1',
                                'filename': 'materials.csv',
                                'preview': {'row_count': 1},
                                'content': raw_content,
                            }
                        ],
                    },
                )
            )

        self.assertIsNone(result.error)
        self.assertTrue(prompts)
        self.assertEqual(result.output_payload['answer'], f'龙粳33:{len(raw_content.encode("utf-8"))}')
        self.assertIs(result.output_payload['raw_content_seen'], True)

    async def test_platform_service_receives_only_sanitized_artifacts_and_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / 'skill'
            skill_dir = root / 'platform-safe'
            skill_dir.mkdir(parents=True)
            (skill_dir / 'SKILL.md').write_text(
                """---
name: platform-safe
description: 平台服务只能看摘要
execution:
  mode: platform_service
  answer_mode: direct
  handler: demo.handler
outputs:
  required:
    - response_text
---

# Platform Safe
平台服务。
""",
                encoding='utf-8',
            )
            state = self._build_state(root)
            raw_content = 'plot_id,hyb_check,set\n1,A,A\n'

            def handler(context):
                serialized_artifacts = str(context.artifact_context)
                serialized_metadata = str(context.safe_metadata)
                return {
                    'response_text': 'safe',
                    'artifact_content_visible': raw_content in serialized_artifacts or '1,A,A' in serialized_artifacts,
                    'metadata_content_visible': raw_content in serialized_metadata or '1,A,A' in serialized_metadata,
                }

            executor = SkillExecutor(
                runtime_state=state,
                platform_handler_registry=SkillPlatformHandlerRegistry(
                    handlers={'demo.handler': handler},
                    trusted_skill_handlers={'skill.platform_safe': 'demo.handler'},
                    trusted_skill_services={'skill.platform_safe': ()},
                ),
                service_registry=SkillServiceRegistry(),
            )
            result = await executor.execute(
                CapabilityExecutionRequest(
                    capability_id='skill.platform_safe',
                    conversation_id='conv-1',
                    task_id='task-1',
                    node_id='node-1',
                    input_payload={'user_message': 'hello'},
                    metadata={
                        'skill_bundle_revision': state.active_revision,
                        'uploaded_artifacts': [
                            {
                                'upload_id': 'upl-1',
                                'filename': 'materials.csv',
                                'preview': {'row_count': 1},
                                'content': raw_content,
                            }
                        ],
                        'skill_artifacts': [
                            {
                                'upload_id': 'upl-1',
                                'filename': 'materials.csv',
                                'preview': {'row_count': 1},
                                'content': raw_content,
                            }
                        ],
                    },
                )
            )

        self.assertIsNone(result.error)
        self.assertIs(result.output_payload['artifact_content_visible'], False)
        self.assertIs(result.output_payload['metadata_content_visible'], False)

    async def test_python_subprocess_direct_answer_still_returns_text_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / 'skill'
            skill_dir = root / 'direct-rcbd'
            scripts = skill_dir / 'scripts'
            scripts.mkdir(parents=True)
            (scripts / 'answer.py').write_text(
                'import json, sys\njson.load(sys.stdin)\nprint(json.dumps({"answer": "直接回答"}, ensure_ascii=False))',
                encoding='utf-8',
            )
            (skill_dir / 'SKILL.md').write_text(
                """---
name: direct-rcbd
description: 直接输出
scripts:
  - name: answer
    path: scripts/answer.py
    runtime: python
execution:
  answer_mode: direct
outputs:
  required:
    - answer
---

# Direct RCBD
执行脚本。
""",
                encoding='utf-8',
            )
            state = self._build_state(root)
            executor = SkillExecutor(runtime_state=state, script_runner=SkillScriptRunner())
            result = await executor.execute(
                CapabilityExecutionRequest(
                    capability_id='skill.direct_rcbd',
                    conversation_id='conv-1',
                    task_id='task-1',
                    node_id='node-1',
                    input_payload={'user_message': '直接输出'},
                    metadata={'skill_bundle_revision': state.active_revision},
                )
            )

        self.assertIsNone(result.error)
        self.assertEqual(result.output_payload['response_text'], '直接回答')
        self.assertEqual([artifact.artifact_type.value for artifact in result.artifacts], ['text'])
        self.assertEqual(result.artifacts[0].storage_ref, '直接回答')

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
        self.assertIsNotNone(result.interrupt)
        self.assertEqual(result.interrupt.reason_code, 'missing_variety')
        self.assertIn('variety', result.interrupt.required_fields)

    async def test_structured_stdout_missing_input_returns_interrupt(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / 'skill'
            skill_dir = root / 'conditional'
            scripts = skill_dir / 'scripts'
            scripts.mkdir(parents=True)
            (scripts / 'needs_file.py').write_text(
                textwrap.dedent(
                    '''
                    import json, sys
                    json.load(sys.stdin)
                    print(json.dumps({
                        "ok": False,
                        "is_error": True,
                        "error": {"type": "missing_input", "message": "缺少田间数据文件。"},
                        "missing": ["field_data"],
                        "answer": "还缺少田间观测数据文件，请上传后继续。",
                    }, ensure_ascii=False))
                    '''
                ).strip(),
                encoding='utf-8',
            )
            (skill_dir / 'SKILL.md').write_text(
                """---
name: conditional
description: 条件缺参
parameters:
  field_data:
    type: artifact
    required: false
scripts:
  - name: needs_file
    path: scripts/needs_file.py
    runtime: python
outputs:
  required:
    - answer
---

# Conditional
执行脚本。
""",
                encoding='utf-8',
            )
            state = self._build_state(root)
            executor = SkillExecutor(runtime_state=state, script_runner=SkillScriptRunner())
            result = await executor.execute(
                CapabilityExecutionRequest(
                    capability_id='skill.conditional',
                    conversation_id='conv-1',
                    task_id='task-1',
                    node_id='node-1',
                    input_payload={'user_message': '分析田间数据'},
                    metadata={'skill_bundle_revision': state.active_revision},
                )
            )

        self.assertIsNone(result.error)
        self.assertIsNotNone(result.interrupt)
        self.assertEqual(result.interrupt.reason_code, 'missing_field_data')
        self.assertIn('field_data', result.interrupt.required_fields)
        self.assertEqual(result.interrupt.required_fields['field_data']['type'], 'artifact')
        self.assertIs(result.interrupt.required_fields['field_data']['accepts_upload'], True)
        self.assertEqual(result.output_payload['missing'], ['field_data'])
        event_types = [event.event_type for event in result.events]
        self.assertIn('skill.input_missing', event_types)
        self.assertNotIn('skill.execution_completed', event_types)

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

    async def test_platform_service_requires_finalizer_normalizes_answer_payload(self) -> None:
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
  answer_mode: requires_finalizer
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
                handlers={'demo.handler': lambda _ctx: {'answer': '平台服务结果'}},
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
        self.assertEqual(result.output_payload['answer'], '平台服务结果')
        self.assertEqual(result.output_payload['response_text'], '平台服务结果')
        self.assertEqual(result.artifacts, ())

    async def test_platform_service_normalizes_failed_answer_payload(self) -> None:
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
  answer_mode: requires_finalizer
  trust_scope: project
  handler: demo.handler
---

# Platform
平台服务。
""",
                encoding='utf-8',
            )
            state = self._build_state(root)
            handlers = SkillPlatformHandlerRegistry(
                handlers={'demo.handler': lambda _ctx: {'ok': False, 'answer': '缺少输入'}},
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

        self.assertIsNone(result.error)
        self.assertEqual(result.output_payload['response_text'], '缺少输入')
        self.assertIs(result.output_payload['is_error'], True)

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

    async def test_platform_service_missing_input_error_is_converted_to_interrupt(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / 'skill'
            skill_dir = root / 'platform'
            skill_dir.mkdir(parents=True)
            (skill_dir / 'SKILL.md').write_text(
                """---
name: platform
description: 平台服务
parameters:
  query:
    type: string
    required: true
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

            async def missing_handler(_ctx):
                from src.core.contracts import CapabilityExecutionError
                from src.integrations.codex_skills.execution import SkillPlatformHandlerResult

                return SkillPlatformHandlerResult(
                    output_payload={'domain_kind': 'demo'},
                    error=CapabilityExecutionError(code='skill_input_missing', message='missing query', retriable=False),
                )

            handlers = SkillPlatformHandlerRegistry(
                handlers={'demo.handler': missing_handler},
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
                    input_payload={'user_message': ''},
                    metadata={'skill_bundle_revision': state.active_revision},
                )
            )

        self.assertIsNotNone(result.error)
        self.assertEqual(result.error.code, 'skill_input_missing')
        self.assertIsNotNone(result.interrupt)
        self.assertIn('query', result.interrupt.required_fields)
        event_types = [event.event_type for event in result.events]
        self.assertIn('skill.input_missing', event_types)
        self.assertIn('skill.execution_interrupted', event_types)
        self.assertNotIn('skill.execution_completed', event_types)

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
