from __future__ import annotations

import _bootstrap  # noqa: F401
import unittest

from sql_query_skill.sql_ast import (
    SQLAstError,
    analyze_sql,
    branch_has_constraint,
    branch_projection_literal,
    final_has_limit,
    final_has_order,
)


class SQLAstTest(unittest.TestCase):
    def test_parses_alias_backtick_and_reverse_comparison(self) -> None:
        analysis = analyze_sql(
            "SELECT rv.`year`, rv.variety_name FROM rice_varieties rv "
            "WHERE 2021 = rv.`year` AND rv.applicant LIKE '%隆平高科%' ORDER BY rv.year DESC LIMIT 10"
        )

        self.assertEqual(analysis.statement_kind, "select")
        self.assertEqual(analysis.tables, ("rice_varieties",))
        self.assertEqual(len(analysis.branches), 1)
        branch = analysis.branches[0]
        self.assertEqual(branch.alias_to_table["rv"], "rice_varieties")
        self.assertTrue(branch_has_constraint(branch, field="year", operator="=", value=2021, table="rice_varieties"))
        self.assertTrue(branch_has_constraint(branch, field="applicant", operator="LIKE", value="隆平高科", table="rice_varieties"))
        self.assertTrue(final_has_order(analysis, field="year", direction="DESC"))
        self.assertTrue(final_has_limit(analysis, 10))

    def test_distinguishes_union_all_branches_and_projection_markers(self) -> None:
        analysis = analyze_sql(
            "SELECT 'applicant' AS matched_field, 'peer' AS match_tier FROM rice_varieties WHERE applicant LIKE '%x%' "
            "UNION ALL "
            "SELECT 'breeder' AS matched_field, 'peer' AS match_tier FROM rice_varieties WHERE breeder LIKE '%x%'"
        )

        self.assertTrue(analysis.has_union)
        self.assertTrue(analysis.is_union_all)
        self.assertEqual(len(analysis.branches), 2)
        self.assertEqual(branch_projection_literal(analysis.branches[0], "matched_field"), "applicant")
        self.assertEqual(branch_projection_literal(analysis.branches[1], "matched_field"), "breeder")
        self.assertTrue(branch_has_constraint(analysis.branches[0], field="applicant", operator="LIKE", value="x", table="rice_varieties"))
        self.assertTrue(branch_has_constraint(analysis.branches[1], field="breeder", operator="LIKE", value="x", table="rice_varieties"))

    def test_wrapped_union_has_final_order_and_limit(self) -> None:
        analysis = analyze_sql(
            "SELECT * FROM ("
            "SELECT year, variety_name FROM rice_varieties WHERE year = 2021 "
            "UNION ALL "
            "SELECT year, variety_name FROM wheat_varieties WHERE year = 2021"
            ") AS constrained_results ORDER BY year DESC LIMIT 10"
        )

        self.assertEqual(analysis.tables, ("rice_varieties", "wheat_varieties"))
        self.assertEqual(len(analysis.branches), 2)
        self.assertFalse(any(branch.has_limit for branch in analysis.branches))
        self.assertTrue(final_has_order(analysis, field="year", direction="DESC"))
        self.assertTrue(final_has_limit(analysis, 10))

    def test_branch_local_limit_is_not_final_limit_for_root_union(self) -> None:
        analysis = analyze_sql(
            "SELECT * FROM rice_varieties LIMIT 10 UNION ALL SELECT * FROM wheat_varieties"
        )

        self.assertTrue(analysis.has_union)
        self.assertEqual(analysis.final_limit, None)
        self.assertTrue(analysis.branches[0].has_limit)

    def test_cte_alias_is_not_reported_as_real_table(self) -> None:
        analysis = analyze_sql("WITH x AS (SELECT * FROM rice_varieties) SELECT * FROM x WHERE year = 2021")

        self.assertIn("rice_varieties", analysis.tables)
        self.assertNotIn("x", analysis.tables)

    def test_cte_alias_without_real_table_is_not_reported_as_table(self) -> None:
        analysis = analyze_sql("WITH x AS (SELECT 1) SELECT * FROM x")

        self.assertEqual(analysis.tables, ())
        self.assertNotIn("x", analysis.tables)

    def test_cte_union_sources_are_expanded_as_branches(self) -> None:
        analysis = analyze_sql(
            "WITH x AS ("
            "SELECT year, variety_name FROM rice_varieties WHERE year = 2021 "
            "UNION ALL "
            "SELECT year, variety_name FROM wheat_varieties WHERE year = 2021"
            ") SELECT * FROM x ORDER BY year DESC LIMIT 10"
        )

        self.assertEqual(analysis.tables, ("rice_varieties", "wheat_varieties"))
        self.assertTrue(analysis.is_union_all)
        self.assertEqual(len(analysis.branches), 2)
        self.assertTrue(branch_has_constraint(analysis.branches[0], field="year", operator="=", value=2021, table="rice_varieties"))
        self.assertTrue(branch_has_constraint(analysis.branches[1], field="year", operator="=", value=2021, table="wheat_varieties"))
        self.assertTrue(final_has_order(analysis, field="year", direction="DESC"))
        self.assertTrue(final_has_limit(analysis, 10))

    def test_cte_outer_filter_is_applied_to_expanded_branches(self) -> None:
        analysis = analyze_sql(
            "WITH x AS ("
            "SELECT year, variety_name FROM rice_varieties "
            "UNION ALL "
            "SELECT year, variety_name FROM wheat_varieties"
            ") SELECT * FROM x WHERE x.year = 2021 ORDER BY year DESC LIMIT 10"
        )

        self.assertEqual(analysis.tables, ("rice_varieties", "wheat_varieties"))
        self.assertEqual(len(analysis.branches), 2)
        self.assertTrue(branch_has_constraint(analysis.branches[0], field="year", operator="=", value=2021, table="rice_varieties"))
        self.assertTrue(branch_has_constraint(analysis.branches[1], field="year", operator="=", value=2021, table="wheat_varieties"))

    def test_parse_error_is_controlled(self) -> None:
        with self.assertRaises(SQLAstError):
            analyze_sql("SELECT FROM")


if __name__ == "__main__":
    unittest.main()
