from __future__ import annotations

import unittest
from pathlib import Path

from src.integrations.agent_skills import SkillCatalog, match_skills


class ProjectSkillRouteAuthorityTest(unittest.TestCase):
    def setUp(self) -> None:
        required = (
            Path("skill/mini_breedstat_rcbd_skill/SKILL.md"),
            Path("skill/field-design/SKILL.md"),
        )
        if not all(path.exists() for path in required):
            self.skipTest("external Mini BreedStat and field-design skills are not present")
        self.catalog = SkillCatalog.from_roots(("skill",))

    def test_mini_rcbd_owns_explicit_rcbd_and_position_constraint_queries(self) -> None:
        queries = (
            "帮我做一个RCBD随机区组田间设计",
            "帮我用上传材料做随机区组，2次重复",
            "请生成随机区组设计 fieldbook",
            "make a randomized complete block design for these materials",
            "按对照位置约束做田间小区排布",
        )

        self._assert_unique_first("mini-breedstat-rcbd", queries)

    def test_field_design_owns_generic_diagonal_interval_and_layout_queries(self) -> None:
        queries = (
            "请做田间试验设计",
            "我要做对角线增广设计，ncols 20",
            "帮我生成 interval contrast design",
            "请生成fieldbook和田间布局预览",
        )

        self._assert_unique_first("field-design", queries)

    def _assert_unique_first(self, expected: str, queries: tuple[str, ...]) -> None:
        for query in queries:
            with self.subTest(query=query):
                matches = match_skills(query, self.catalog, max_matches=5)
                self.assertTrue(matches)
                self.assertGreater(matches[0].score, 0)
                self.assertEqual(matches[0].manifest.name, expected)
                if len(matches) > 1:
                    self.assertGreater(
                        matches[0].score,
                        matches[1].score,
                        "route authority must not depend on alphabetical tie-breaking",
                    )


if __name__ == "__main__":
    unittest.main()
