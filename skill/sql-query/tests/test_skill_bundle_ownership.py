from __future__ import annotations

import unittest
from pathlib import Path

import _bootstrap  # noqa: F401
import yaml


REPO_ROOT = Path(__file__).resolve().parents[3]
SKILL_ROOT = REPO_ROOT / "skill" / "sql-query"
FORBIDDEN_SYSTEM_PATTERNS = (
    "SQLQuery",
    "sql-query",
    "skill.sql_query",
    "src.sql_query",
    "src/sql_query",
    "configs/sql_query",
    "tests/sql_query",
    "SQLQueryPlatformHandler",
    "SQLQueryWorkflowProvider",
    "SQLQueryExecutor",
    "SQLQueryRuntimeReplanner",
    "skill.sql_query.platform_handler",
    "llm.sql_query",
)
FORBIDDEN_CHANGELOG_PATTERNS = (
    "src.sql_query",
    "src/sql_query",
    "configs/sql_query",
    "tests/sql_query",
    "SQLQueryPlatformHandler",
    "skill.sql_query.platform_handler",
    "llm.sql_query",
)


class SQLQuerySkillBundleOwnershipTest(unittest.TestCase):
    def test_sqlquery_system_implementation_dirs_are_removed(self) -> None:
        for relative in ("src/sql_query", "configs/sql_query", "tests/sql_query", "src/capabilities/sql_query", "tests/capabilities/sql_query"):
            self.assertFalse((REPO_ROOT / relative).exists(), relative)

    def test_sqlquery_runtime_config_and_tests_live_inside_skill_bundle(self) -> None:
        self.assertTrue((SKILL_ROOT / "runtime" / "sql_query_skill" / "engine.py").is_file())
        self.assertTrue((SKILL_ROOT / "configs" / "routing_rules.yaml").is_file())
        self.assertTrue((SKILL_ROOT / "tests" / "test_engine.py").is_file())

    def test_system_code_and_tests_do_not_hardcode_sqlquery_bundle_details(self) -> None:
        offenders: list[str] = []
        for root_name in ("src", "tests", "frontend/src", "scripts"):
            root = REPO_ROOT / root_name
            if not root.exists():
                continue
            for path in root.rglob("*"):
                if not path.is_file() or "__pycache__" in path.parts:
                    continue
                text = path.read_text(encoding="utf-8", errors="ignore")
                if any(pattern in text for pattern in FORBIDDEN_SYSTEM_PATTERNS):
                    offenders.append(str(path.relative_to(REPO_ROOT)))
        self.assertEqual(offenders, [])

    def test_active_docs_do_not_point_to_system_owned_sqlquery_runtime(self) -> None:
        offenders: list[str] = []
        for root_name in ("docs",):
            for path in (REPO_ROOT / root_name).rglob("*.md"):
                rel = path.relative_to(REPO_ROOT).as_posix()
                text = path.read_text(encoding="utf-8", errors="ignore")
                if any(pattern in text for pattern in FORBIDDEN_SYSTEM_PATTERNS):
                    offenders.append(rel)
        for file_name in ("README.md", "AGENTS.md", "Codex-Skill构建指南.md"):
            path = REPO_ROOT / file_name
            text = path.read_text(encoding="utf-8", errors="ignore")
            if any(pattern in text for pattern in FORBIDDEN_SYSTEM_PATTERNS):
                offenders.append(file_name)
        changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8", errors="ignore")
        if any(pattern in changelog for pattern in FORBIDDEN_CHANGELOG_PATTERNS):
            offenders.append("CHANGELOG.md")
        self.assertEqual(offenders, [])

    def test_manifest_uses_project_bundle_handler_and_generic_llm_service(self) -> None:
        manifest = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("capability_id: skill.sql_query", manifest)
        self.assertIn("handler_module: runtime/sql_query_skill/platform_handler.py", manifest)
        self.assertIn("handler_factory: build_handler", manifest)
        self.assertIn("- llm.non_stream", manifest)
        self.assertNotIn("llm.sql_query", manifest)

    def test_manifest_triggers_are_domain_specific_not_generic_query_words(self) -> None:
        manifest = next(yaml.safe_load_all((SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")))

        triggers = set(manifest["triggers"])

        self.assertNotIn("查一下", triggers)
        self.assertNotIn("查询", triggers)
        self.assertNotIn("数据库查询", triggers)
        self.assertTrue({"查询品种", "审定信息", "基因型"}.issubset(triggers))

    def test_routing_config_exposes_only_supported_database_routes(self) -> None:
        routing_rules = yaml.safe_load((SKILL_ROOT / "configs" / "routing_rules.yaml").read_text(encoding="utf-8"))
        schema_metadata = yaml.safe_load((SKILL_ROOT / "configs" / "schema_metadata.yaml").read_text(encoding="utf-8"))

        route_ids = [route["route_id"] for route in routing_rules["routes"]]
        profile_route_ids = [profile["route_id"] for profile in schema_metadata["schema_profiles"]]

        self.assertEqual(route_ids, ["approval_variety_db", "genotype_db"])
        self.assertNotIn("variety_overview", route_ids)
        self.assertNotIn("variety_overview", profile_route_ids)


if __name__ == "__main__":
    unittest.main()
