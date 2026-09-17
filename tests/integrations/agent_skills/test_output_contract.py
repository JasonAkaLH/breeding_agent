from __future__ import annotations

import textwrap

from tests.api.support import APITestCase


class OutputContractV2APITest(APITestCase):
    async def test_output_contract_failure_is_committed_for_agent_recovery(self) -> None:
        root = self.workspace / "skill"
        skill = root / "bad-output"
        (skill / "scripts").mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: bad-output\ndescription: bad\n---\n\n# Bad\n", encoding="utf-8")
        (skill / "skill.contract.yaml").write_text("""
contract_version: '2'
capability: {id: skill.bad_output, display_name: Bad Output}
runtime: {mode: python_subprocess, answer_mode: direct}
entrypoints:
  run: {path: scripts/run.py, output: out}
outputs:
  out: {required: [answer]}
""", encoding="utf-8")
        (skill / "scripts" / "run.py").write_text("import json\nprint(json.dumps({'not_answer':'x'}))\n", encoding="utf-8")
        await self.reconfigure_runtime(skill_roots=(root,), public_skill_roots=(root,))
        response = await self.submit_message(content="run", capability_id="skill.bad_output")
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]
        terminal = await self.wait_for_terminal_task(task_id)
        self.assertEqual(terminal["status"], "completed")
        events = await self.runtime.storage.list_events_for_task(task_id)
        validation = next(event for event in events if event.event_type == "skill.output_contract_validated")
        self.assertFalse(validation.payload["schema_validated"])
        self.assertEqual(validation.payload["missing"], ["answer"])
        nodes = await self.runtime.storage.list_task_nodes_for_task(task_id)
        skill_node = next(node for node in nodes if node.capability_id == "skill.bad_output")
        self.assertEqual(str(skill_node.status), "failed")
