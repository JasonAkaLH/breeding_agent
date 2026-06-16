from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.integrations.agent_skills import load_input_schemas_for_contract, parse_skill_contract_file, select_input_schema


class InputSchemaSelectorTest(unittest.TestCase):
    def _fixture(self):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        (root / "schemas").mkdir()
        for schema_id, title in [("rcbd", "随机区组 RCBD"), ("diagonal", "对角线 Diagonal"), ("interval", "间比法 Interval")]:
            (root / "schemas" / f"{schema_id}.input.yaml").write_text(
                f"schema_id: {schema_id}\ntitle: {title}\ninputs:\n  design: {{type: string, required: true}}\n",
                encoding="utf-8",
            )
        (root / "skill.contract.yaml").write_text(
            """
contract_version: '2'
capability: {id: skill.field_design, display_name: Field Design}
runtime: {mode: python_subprocess}
schema_selector: {strategy: deterministic_then_llm, selector_field: design, min_confidence: 0.8}
entrypoints:
  run: {path: scripts/run.py}
input_schemas:
  rcbd: {path: schemas/rcbd.input.yaml, aliases: [rcbd, 随机区组], entrypoint: run}
  diagonal: {path: schemas/diagonal.input.yaml, aliases: [diagonal, 对角线], entrypoint: run}
  interval: {path: schemas/interval.input.yaml, aliases: [interval, 间比法], entrypoint: run}
""",
            encoding="utf-8",
        )
        contract = parse_skill_contract_file(root / "skill.contract.yaml")
        return tmp, contract, load_input_schemas_for_contract(contract)

    def test_deterministic_unique_match_selects_schema(self) -> None:
        tmp, contract, schemas = self._fixture()
        self.addCleanup(tmp.cleanup)
        result = select_input_schema(contract, schemas, query="请做间比法设计")
        self.assertTrue(result.selected)
        self.assertEqual(result.selected_schema_id, "interval")
        self.assertEqual(result.selected_entrypoint, "run")

    def test_user_text_schema_match_beats_weak_artifact_filename_match(self) -> None:
        tmp, contract, schemas = self._fixture()
        self.addCleanup(tmp.cleanup)
        result = select_input_schema(
            contract,
            schemas,
            query="你帮我设计一个对角线增广试验",
            artifact_summaries=(
                {
                    "upload_id": "upl-interval-name",
                    "filename": "interval_realistic_two_sets.csv",
                    "preview": {"columns": ["ped_id", "hyb_check", "set"], "row_count": 60},
                },
            ),
        )

        self.assertTrue(result.selected)
        self.assertEqual(result.selected_schema_id, "diagonal")
        self.assertEqual(result.reason, "deterministic_alias")

    def test_ambiguous_request_does_not_select(self) -> None:
        tmp, contract, schemas = self._fixture()
        self.addCleanup(tmp.cleanup)
        result = select_input_schema(contract, schemas, query="做田间试验设计")
        self.assertFalse(result.selected)
        self.assertEqual(result.missing_selector_field, "design")

    def test_resume_pinned_schema_bypasses_selector(self) -> None:
        tmp, contract, schemas = self._fixture()
        self.addCleanup(tmp.cleanup)
        result = select_input_schema(contract, schemas, query="普通描述", metadata={"skill_slot_collection": {"selected_schema_id": "rcbd"}})
        self.assertTrue(result.selected)
        self.assertEqual(result.reason, "resume_pinned")

    def test_llm_selector_candidate_is_allowlist_and_confidence_checked(self) -> None:
        tmp, contract, schemas = self._fixture()
        self.addCleanup(tmp.cleanup)
        ok = select_input_schema(contract, schemas, query="需要设计", llm_text_generator=lambda _p: '{"schema_id":"rcbd","confidence":0.9}')
        self.assertEqual(ok.selected_schema_id, "rcbd")
        unknown = select_input_schema(contract, schemas, query="需要设计", llm_text_generator=lambda _p: '{"schema_id":"hack","confidence":0.99}')
        self.assertEqual(unknown.reason, "llm_schema_not_allowed")
        low = select_input_schema(contract, schemas, query="需要设计", llm_text_generator=lambda _p: '{"schema_id":"rcbd","confidence":0.2}')
        self.assertEqual(low.reason, "llm_low_confidence")
        bad = select_input_schema(contract, schemas, query="需要设计", llm_text_generator=lambda _p: 'not-json')
        self.assertEqual(bad.reason, "llm_invalid_json")
