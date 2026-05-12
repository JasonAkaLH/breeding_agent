from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.integrations.codex_skills import parse_skill_file
from src.integrations.codex_skills.execution import SkillExecutionConfigError, resolve_skill_execution_config
from src.integrations.codex_skills.skill_runtime_state import SkillRuntimeState


class SkillExecutionConfigTest(unittest.TestCase):
    def _write_skill(self, root: Path, body: str) -> Path:
        path = root / 'SKILL.md'
        path.write_text(body, encoding='utf-8')
        return path

    def test_instruction_only_skill_defaults_to_delegated_main_agent_direct(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_file = self._write_skill(
                Path(tmpdir),
                """---
name: report-writer
description: 写报告
---

# Report Writer
请写报告。
""",
            )
            manifest = parse_skill_file(skill_file)

        config = resolve_skill_execution_config(manifest)
        self.assertEqual(config.mode, 'delegated_main_agent')
        self.assertEqual(config.answer_mode, 'direct')
        self.assertEqual(config.trust_scope, '')
        self.assertEqual(config.handler, '')
        self.assertEqual(config.services, ())

    def test_script_skill_defaults_to_python_subprocess_requires_finalizer(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_file = self._write_skill(
                Path(tmpdir),
                """---
name: scripted
description: 处理文本
scripts:
  - name: echo
    path: scripts/echo.py
    runtime: python
---

# Scripted
运行脚本。
""",
            )
            manifest = parse_skill_file(skill_file)

        config = resolve_skill_execution_config(manifest)
        self.assertEqual(config.mode, 'python_subprocess')
        self.assertEqual(config.answer_mode, 'requires_finalizer')

    def test_platform_service_requires_explicit_answer_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_file = self._write_skill(
                Path(tmpdir),
                """---
name: sql-query
description: 查询数据库
execution:
  mode: platform_service
  handler: sql_query.query
  trust_scope: project
  services:
    - mysql_readonly
---

# SQL Query
查询数据库。
""",
            )
            manifest = parse_skill_file(skill_file)

        with self.assertRaises(SkillExecutionConfigError):
            resolve_skill_execution_config(manifest)

    def test_runtime_state_exposes_skill_payload_policies(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / 'skill'
            skill_dir = root / 'scripted'
            skill_dir.mkdir(parents=True)
            (skill_dir / 'SKILL.md').write_text(
                """---
name: scripted
description: 处理文本
scripts:
  - name: echo
    path: scripts/echo.py
    runtime: python
---

# Scripted
运行脚本。
""",
                encoding='utf-8',
            )
            state = SkillRuntimeState.from_roots(
                skill_roots=(root,),
                public_skill_roots=(root,),
                reserved_capability_ids=('main_agent.respond', 'sql_query.query'),
            )

        policy = state.active_bundle.skill_capabilities.payload_policies['skill.scripted']
        self.assertEqual(policy.planner_allowed_fields, ('subtask_label', 'parent_question'))


if __name__ == '__main__':
    unittest.main()
