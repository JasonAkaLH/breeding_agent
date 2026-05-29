from __future__ import annotations

import unittest

from src.capabilities.main_agent.prompt_builder import build_dependency_context, build_main_agent_prompt


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

    def test_platform_file_descriptors_enter_dependency_context(self) -> None:
        context = build_dependency_context(
            {
                "node-1": {
                    "response_text": "RCBD 设计已完成",
                    "output_files": [
                        {
                            "artifact_id": "artifact-1",
                            "filename": "fieldbook.csv",
                            "mime_type": "text/csv",
                            "summary": "完整 fieldbook CSV",
                            "download_url": "/api/v1/artifacts/artifact-1/download",
                            "size_bytes": 128,
                            "path": "outputs/internal.csv",
                        }
                    ],
                }
            }
        )

        self.assertEqual(
            context,
            [
                {
                    "node_id": "node-1",
                    "response_text": "RCBD 设计已完成",
                    "output_files": [
                        {
                            "artifact_id": "artifact-1",
                            "filename": "fieldbook.csv",
                            "mime_type": "text/csv",
                            "summary": "完整 fieldbook CSV",
                            "download_url": "/api/v1/artifacts/artifact-1/download",
                            "size_bytes": 128,
                        }
                    ],
                }
            ],
        )

    def test_raw_answer_is_not_dependency_context_contract(self) -> None:
        context = build_dependency_context({"node-1": {"answer": "RCBD 完成"}})

        self.assertEqual(context, [])

    def test_prompt_contains_strict_file_download_constraints(self) -> None:
        prompt = build_main_agent_prompt(
            user_message="生成 CSV 文件",
            skill_matches=[],
            artifact_context=[],
            script_results=[
                {
                    "skill_name": "field-design",
                    "entrypoint": "run_field_design",
                    "output": {
                        "ok": False,
                        "is_error": True,
                        "response_text": "试验设计执行失败：Input data is missing required columns: ped_id",
                    },
                }
            ],
        )

        self.assertIn("文件和下载链接硬约束", prompt)
        self.assertIn("output_files", prompt)
        self.assertIn("/api/v1/artifacts/", prompt)
        self.assertIn("sandbox:/mnt/data", prompt)
        self.assertIn("is_error", prompt)
        self.assertIn("不得声称文件已生成", prompt)


if __name__ == "__main__":
    unittest.main()
