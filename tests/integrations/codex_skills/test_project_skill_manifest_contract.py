from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any

from src.integrations.codex_skills import parse_skill_file


class ProjectSkillManifestContractTest(unittest.TestCase):
    def test_all_project_skills_declare_display_name(self) -> None:
        skill_files = sorted(Path("skill").glob("*/SKILL.md"))
        self.assertGreater(len(skill_files), 0)
        missing: list[str] = []
        for skill_file in skill_files:
            manifest = parse_skill_file(skill_file)
            display_name = manifest.metadata.get("display_name")
            if not isinstance(display_name, str) or not display_name.strip():
                missing.append(str(skill_file))
        self.assertEqual(missing, [])

    def test_all_project_skills_declare_public_usage(self) -> None:
        skill_files = sorted(Path("skill").glob("*/SKILL.md"))
        self.assertGreater(len(skill_files), 0)
        missing: list[str] = []
        incomplete: list[str] = []
        for skill_file in skill_files:
            manifest = parse_skill_file(skill_file)
            public_usage = manifest.metadata.get("public_usage")
            if not isinstance(public_usage, dict) or not public_usage:
                missing.append(str(skill_file))
                continue
            required_sections = ("overview", "input_formats", "examples", "outputs")
            if any(not public_usage.get(section) for section in required_sections):
                incomplete.append(str(skill_file))

        self.assertEqual(missing, [])
        self.assertEqual(incomplete, [])

    def test_project_skill_public_usage_does_not_expose_internal_details(self) -> None:
        skill_files = sorted(Path("skill").glob("*/SKILL.md"))
        forbidden_tokens = (
            "source_path",
            "scripts/run_",
            ".py",
            "Rscript",
            "wrapper",
            "platform_service",
            "handler",
            "sidecar",
            "socket",
            "token",
            "secret",
            "postgresql://",
            "mysql://",
            "/Users/",
        )
        leaks: list[str] = []
        for skill_file in skill_files:
            manifest = parse_skill_file(skill_file)
            public_usage = manifest.metadata.get("public_usage")
            text = _flatten(public_usage)
            for token in forbidden_tokens:
                if token.lower() in text.lower():
                    leaks.append(f"{skill_file}: {token}")

        self.assertEqual(leaks, [])


def _flatten(value: Any) -> str:
    if isinstance(value, dict):
        return "\n".join(f"{key}\n{_flatten(item)}" for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return "\n".join(_flatten(item) for item in value)
    return str(value)
