from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.integrations.agent_skills.contract import SkillContractParseError, parse_skill_contract_file


class SkillContractParserTest(unittest.TestCase):
    def _write_contract(self, root: Path, body: str) -> Path:
        path = root / "skill.contract.yaml"
        path.write_text(body, encoding="utf-8")
        return path

    def test_parses_python_subprocess_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "scripts").mkdir()
            path = self._write_contract(
                root,
                """
contract_version: '2'
capability:
  id: skill.demo
  display_name: Demo Skill
  description: Runs demo
runtime:
  mode: python_subprocess
  answer_mode: direct
entrypoints:
  run:
    path: scripts/run.py
    input_schema: demo
    output: demo_output
input_schemas:
  demo:
    path: schemas/demo.input.yaml
    title: Demo input
outputs:
  demo_output:
    required: [response_text]
resources:
  usage:
    path: references/usage.md
    audience: [main_agent, slot_question]
""",
            )

            contract = parse_skill_contract_file(path)

        self.assertEqual(contract.capability.id, "skill.demo")
        self.assertEqual(contract.runtime.mode, "python_subprocess")
        self.assertEqual(contract.entrypoints["run"].path, "scripts/run.py")
        self.assertEqual(contract.input_schemas["demo"].path, "schemas/demo.input.yaml")
        self.assertEqual(contract.outputs["demo_output"].required, ("response_text",))
        self.assertEqual(contract.resources["usage"].audience, ("main_agent", "slot_question"))

    def test_parses_final_file_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            path = self._write_contract(
                root,
                """
contract_version: '2'
capability:
  id: skill.demo
  display_name: Demo Skill
runtime:
  mode: python_subprocess
entrypoints:
  run:
    path: scripts/run.py
    input_schema: demo
file_selection:
  required: true
  allow_multiple: true
  expected_content: [材料表]
  supported_file_types: [csv, spreadsheet]
  helpful_columns: [ped_id]
  disambiguation_hint: 优先选择材料表。
input_schemas:
  demo:
    path: schemas/demo.input.yaml
""",
            )

            contract = parse_skill_contract_file(path)

        self.assertTrue(contract.file_selection.required)
        self.assertTrue(contract.file_selection.allow_multiple)
        self.assertEqual(contract.file_selection.expected_content, ("材料表",))
        self.assertEqual(contract.file_selection.supported_file_types, ("csv", "spreadsheet"))
        self.assertEqual(contract.file_selection.helpful_columns, ("ped_id",))
        self.assertEqual(contract.file_selection.disambiguation_hint, "优先选择材料表。")

    def test_rejects_legacy_file_intent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            path = self._write_contract(
                root,
                """
contract_version: '2'
capability:
  id: skill.demo
  display_name: Demo Skill
runtime:
  mode: python_subprocess
entrypoints:
  run:
    path: scripts/run.py
file_intent:
  requires_file: true
""",
            )

            with self.assertRaisesRegex(SkillContractParseError, "file_intent"):
                parse_skill_contract_file(path)

    def test_parses_platform_service_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            path = self._write_contract(
                root,
                """
contract_version: '2'
capability:
  id: skill.sql_query
  display_name: SQL Query
runtime:
  mode: platform_service
  answer_mode: direct
  trust_scope: project
  handler_module: runtime/sql_query_skill/platform_handler.py
  services: [mysql]
entrypoints:
  query:
    runtime: platform_service
    output: query_output
outputs:
  query_output:
    required: [response_text]
""",
            )

            contract = parse_skill_contract_file(path)

        self.assertEqual(contract.runtime.mode, "platform_service")
        self.assertEqual(contract.entrypoints["query"].handler_module, "runtime/sql_query_skill/platform_handler.py")
        self.assertEqual(contract.entrypoints["query"].services, ("mysql",))

    def test_missing_required_fields_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = self._write_contract(Path(tmpdir), "capability: {id: skill.demo}\n")
            with self.assertRaises(SkillContractParseError):
                parse_skill_contract_file(path)

    def test_entrypoint_output_ref_must_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            path = self._write_contract(
                root,
                """
contract_version: '2'
capability: {id: skill.demo, display_name: Demo}
runtime: {mode: python_subprocess}
entrypoints:
  run: {path: scripts/run.py, output: missing}
""",
            )
            with self.assertRaises(SkillContractParseError):
                parse_skill_contract_file(path)

    def test_path_traversal_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            path = self._write_contract(
                root,
                """
contract_version: '2'
capability: {id: skill.demo, display_name: Demo}
runtime: {mode: python_subprocess}
entrypoints:
  run: {path: ../run.py}
""",
            )
            with self.assertRaises(SkillContractParseError):
                parse_skill_contract_file(path)


if __name__ == "__main__":
    unittest.main()
