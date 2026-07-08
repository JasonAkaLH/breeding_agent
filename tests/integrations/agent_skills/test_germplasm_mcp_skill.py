from __future__ import annotations

import unittest

from src.integrations.agent_skills import load_input_schemas_for_contract, parse_skill_file, select_input_schema


class GermplasmMCPSkillTest(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = parse_skill_file("skill/germplasm-mcp/SKILL.md")
        assert self.manifest.contract is not None
        self.contract = self.manifest.contract
        self.schemas = load_input_schemas_for_contract(self.contract)

    def test_contract_registers_delegated_mcp_runbook(self) -> None:
        self.assertEqual(self.contract.capability.id, "skill.germplasm_mcp")
        self.assertEqual(self.contract.runtime.mode, "delegated_main_agent")
        self.assertEqual(set(self.contract.input_schemas), {"list_crops", "list_traits", "list_fields", "germ_search"})
        self.assertEqual(set(self.contract.resources), {"tool_guide", "search_examples", "context_and_errors"})

    def test_common_queries_select_expected_schema(self) -> None:
        cases = {
            "查询当前租户有哪些作物": "list_crops",
            "作物 12 有哪些作物性状": "list_traits",
            "种质扩展字段有哪些": "list_fields",
            "按名称搜索种质 ABC": "germ_search",
        }
        for query, expected_schema in cases.items():
            with self.subTest(query=query):
                result = select_input_schema(self.contract, self.schemas, query=query)
                self.assertEqual(result.selected_schema_id, expected_schema)
                self.assertEqual(result.reason, "deterministic_alias")


if __name__ == "__main__":
    unittest.main()
