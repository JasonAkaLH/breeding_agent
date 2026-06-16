from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.integrations.agent_skills import SkillCatalog
from src.integrations.agent_skills.skill_capabilities import build_skill_capability_registry


class SkillCapabilityMappingTest(unittest.TestCase):
    def _write_skill(
        self,
        root: Path,
        dirname: str,
        *,
        name: str,
        description: str = "生成设计",
        contract: str | None = None,
        frontmatter_extra: str = "",
    ) -> Path:
        skill_dir = root / dirname
        skill_dir.mkdir(parents=True)
        extra = f"\n{frontmatter_extra.rstrip()}" if frontmatter_extra else ""
        (skill_dir / "SKILL.md").write_text(
            f"""---
name: {name}
description: {description}
triggers:
  - 随机区组{extra}
---

# {name}
""",
            encoding="utf-8",
        )
        if contract is None:
            contract = f"""
contract_version: '2'
capability:
  id: skill.{name.replace('-', '_').replace(' ', '_')}
  display_name: 田间试验设计
  description: {description}
runtime:
  mode: python_subprocess
  answer_mode: direct
entrypoints:
  run:
    path: scripts/run.py
"""
        if contract:
            (skill_dir / "skill.contract.yaml").write_text(contract, encoding="utf-8")
        return skill_dir / "SKILL.md"

    def test_project_skill_becomes_public_skill_capability_from_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir) / "skill"
            self._write_skill(project_root, "rcbd", name="mini-breedstat-rcbd", description="生成 RCBD 随机区组设计")
            catalog = SkillCatalog.from_roots([project_root])

            registry = build_skill_capability_registry(catalog, public_skill_roots=(project_root,))

        self.assertEqual(tuple(registry.descriptors_by_id), ("skill.mini_breedstat_rcbd",))
        descriptor = registry.descriptors_by_id["skill.mini_breedstat_rcbd"]
        self.assertEqual(descriptor.name, "mini-breedstat-rcbd")
        self.assertEqual(descriptor.display_name, "田间试验设计")
        self.assertEqual(descriptor.kind, "skill")
        self.assertEqual(descriptor.source, "skill")
        self.assertEqual(descriptor.source_path, "rcbd/SKILL.md")
        self.assertTrue(descriptor.public)
        self.assertEqual(registry.skill_name_by_capability_id["skill.mini_breedstat_rcbd"], "mini-breedstat-rcbd")

    def test_user_skill_outside_public_roots_is_not_registered(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir) / "skill"
            user_root = Path(tmpdir) / "user-skills"
            self._write_skill(project_root, "public", name="public-skill")
            self._write_skill(user_root, "private", name="private-skill")
            catalog = SkillCatalog.from_roots([project_root, user_root])

            registry = build_skill_capability_registry(catalog, public_skill_roots=(project_root,))

        self.assertEqual(set(registry.descriptors_by_id), {"skill.public_skill"})
        skipped = {diagnostic.skill_name: diagnostic.reason for diagnostic in registry.diagnostics}
        self.assertEqual(skipped["private-skill"], "not_public_scope")

    def test_invalid_and_duplicate_capability_ids_are_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir) / "skill"
            self._write_skill(project_root, "invalid", name="bad", contract="""
contract_version: '2'
capability: {id: main_agent.respond, display_name: Bad}
runtime: {mode: python_subprocess}
entrypoints: {run: {path: scripts/run.py}}
""")
            duplicate_contract = """
contract_version: '2'
capability: {id: skill.same, display_name: Same}
runtime: {mode: python_subprocess}
entrypoints: {run: {path: scripts/run.py}}
"""
            self._write_skill(project_root, "first", name="same name", contract=duplicate_contract)
            self._write_skill(project_root, "second", name="same_name", contract=duplicate_contract)
            catalog = SkillCatalog.from_roots([project_root])

            registry = build_skill_capability_registry(catalog, public_skill_roots=(project_root,))

        self.assertEqual(set(registry.descriptors_by_id), set())
        reasons = sorted(diagnostic.reason for diagnostic in registry.diagnostics)
        self.assertEqual(reasons, ["duplicate", "duplicate", "invalid_id"])

    def test_missing_contract_is_not_public_capability(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir) / "skill"
            self._write_skill(project_root, "legacy", name="legacy-helper", contract="")
            catalog = SkillCatalog.from_roots([project_root])

            registry = build_skill_capability_registry(catalog, public_skill_roots=(project_root,))

        self.assertEqual(set(registry.descriptors_by_id), set())
        self.assertEqual(registry.diagnostics[0].skill_name, "legacy-helper")
        self.assertEqual(registry.diagnostics[0].reason, "contract_missing")

    def test_v1_platform_fields_fail_closed_for_v2_skill(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir) / "skill"
            self._write_skill(
                project_root,
                "bad-v2",
                name="bad-v2",
                frontmatter_extra="""
parameters:
  query: {type: string, required: true}
""",
            )
            catalog = SkillCatalog.from_roots([project_root])

            registry = build_skill_capability_registry(catalog, public_skill_roots=(project_root,))

        self.assertEqual(set(registry.descriptors_by_id), set())
        self.assertEqual(registry.diagnostics[0].reason, "v1_field_forbidden")


if __name__ == "__main__":
    unittest.main()
