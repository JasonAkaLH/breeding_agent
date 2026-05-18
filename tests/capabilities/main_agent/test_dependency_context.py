from __future__ import annotations

import unittest

from src.capabilities.main_agent.prompt_builder import build_dependency_context


class MainAgentDependencyContextTest(unittest.TestCase):
    def test_response_text_enters_dependency_context(self) -> None:
        context = build_dependency_context({"node-1": {"response_text": "RCBD 完成"}})

        self.assertEqual(context, [{"node_id": "node-1", "response_text": "RCBD 完成"}])

    def test_domain_large_objects_are_not_injected_without_safe_summary(self) -> None:
        context = build_dependency_context(
            {
                "node-1": {
                    "parameters": {"blocks": 3},
                    "out_design": [{"plot": 1}],
                    "output_files": [{"path": "outputs/layout.html"}],
                }
            }
        )

        self.assertEqual(context, [])

    def test_raw_answer_is_not_dependency_context_contract(self) -> None:
        context = build_dependency_context({"node-1": {"answer": "RCBD 完成"}})

        self.assertEqual(context, [])


if __name__ == "__main__":
    unittest.main()
