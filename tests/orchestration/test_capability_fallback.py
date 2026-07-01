from __future__ import annotations

import unittest

from src.orchestration.capability_fallback import (
    build_capability_missing_fallback_metadata,
    ensure_fallback_disclosure,
)


class CapabilityFallbackTest(unittest.TestCase):
    def test_disclosure_removes_generated_file_and_skill_claims(self) -> None:
        metadata = build_capability_missing_fallback_metadata(
            reason_code="skill_missing",
            missing_capability_summary="缺少田间图 Skill",
            fallback_content_scope="只能给出手工建议",
        )

        text = ensure_fallback_disclosure(
            (
                "文件已生成，请点击下载。后台正在生成报告。已调用 Skill 完成处理。"
                "下载链接如下：https://example.test/file.csv。报告见附件。已完成田间图。"
                "你可以手工整理材料清单。"
            ),
            metadata,
        )

        self.assertIn("【能力缺口说明】", text)
        self.assertIn("不会声称已有文件产物", text)
        self.assertIn("你可以手工整理材料清单", text)
        for forbidden in ("文件已生成", "点击下载", "后台正在生成", "已调用 Skill", "下载链接", "见附件", "已完成田间图"):
            self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main()
