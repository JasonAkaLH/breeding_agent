from __future__ import annotations

import unittest
from pathlib import Path

from src.integrations.agent_skills import parse_skill_file


class ProjectSkillManifestContractTest(unittest.TestCase):
    def test_all_project_skills_declare_contract_display_name(self) -> None:
        skill_files = sorted(Path("skill").glob("*/SKILL.md"))
        self.assertGreater(len(skill_files), 0)
        missing: list[str] = []
        for skill_file in skill_files:
            manifest = parse_skill_file(skill_file)
            if manifest.contract is None or not manifest.contract.capability.display_name.strip():
                missing.append(str(skill_file))
        self.assertEqual(missing, [])

    def test_all_project_skills_declare_public_resources_and_schemas(self) -> None:
        missing_resources: list[str] = []
        missing_schema_or_platform: list[str] = []
        for skill_file in sorted(Path("skill").glob("*/SKILL.md")):
            manifest = parse_skill_file(skill_file)
            contract = manifest.contract
            if contract is None:
                missing_resources.append(str(skill_file))
                continue
            if not contract.resources:
                missing_resources.append(str(skill_file))
            if contract.runtime.mode != "platform_service" and not contract.input_schemas:
                missing_schema_or_platform.append(str(skill_file))
        self.assertEqual(missing_resources, [])
        self.assertEqual(missing_schema_or_platform, [])

    def test_project_skill_lightweight_skill_md_does_not_expose_v1_platform_fields(self) -> None:
        forbidden_tokens = ("capability_id:", "public_usage:", "\nparameters:", "\nscripts:", "\nexecution:", "auto_run", "run_by_default")
        leaks: list[str] = []
        for skill_file in sorted(Path("skill").glob("*/SKILL.md")):
            text = skill_file.read_text(encoding="utf-8")
            for token in forbidden_tokens:
                if token in text:
                    leaks.append(f"{skill_file}: {token.strip()}")
        self.assertEqual(leaks, [])


if __name__ == "__main__":
    unittest.main()
