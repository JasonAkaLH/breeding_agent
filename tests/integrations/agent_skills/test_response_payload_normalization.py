from __future__ import annotations

import unittest

from src.integrations.agent_skills.execution import normalize_skill_response_payload


class SkillResponsePayloadNormalizationTest(unittest.TestCase):
    def test_answer_is_normalized_to_response_text_without_removing_original(self) -> None:
        payload = {"answer": "RCBD 完成"}

        normalized = normalize_skill_response_payload(payload)

        self.assertEqual(normalized["response_text"], "RCBD 完成")
        self.assertEqual(normalized["answer"], "RCBD 完成")
        self.assertNotIn("response_text", payload)

    def test_summary_is_normalized_to_response_text(self) -> None:
        normalized = normalize_skill_response_payload({"summary": "查询完成"})

        self.assertEqual(normalized["response_text"], "查询完成")
        self.assertEqual(normalized["summary"], "查询完成")

    def test_existing_response_text_is_not_overwritten_by_answer(self) -> None:
        normalized = normalize_skill_response_payload({"response_text": "已有文本", "answer": "备用"})

        self.assertEqual(normalized["response_text"], "已有文本")
        self.assertEqual(normalized["answer"], "备用")

    def test_blank_text_does_not_create_response_text(self) -> None:
        normalized = normalize_skill_response_payload({"answer": "   "})

        self.assertNotIn("response_text", normalized)

    def test_failed_payload_sets_is_error_and_preserves_readable_answer(self) -> None:
        normalized = normalize_skill_response_payload({"ok": False, "answer": "缺少材料文件"})

        self.assertEqual(normalized["response_text"], "缺少材料文件")
        self.assertIs(normalized["is_error"], True)

    def test_failed_payload_overrides_conflicting_is_error_flag(self) -> None:
        normalized = normalize_skill_response_payload({"ok": False, "is_error": False, "answer": "缺少材料文件"})

        self.assertIs(normalized["is_error"], True)

    def test_domain_objects_are_not_promoted_to_structured_content(self) -> None:
        normalized = normalize_skill_response_payload(
            {
                "answer": "完成",
                "parameters": {"blocks": 3},
                "out_design": [{"plot": 1}],
                "output_files": [{"path": "outputs/layout.html"}],
            }
        )

        self.assertEqual(normalized["response_text"], "完成")
        self.assertIn("parameters", normalized)
        self.assertIn("out_design", normalized)
        self.assertIn("output_files", normalized)
        self.assertNotIn("structured_content", normalized)


if __name__ == "__main__":
    unittest.main()
