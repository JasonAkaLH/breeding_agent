from __future__ import annotations

import json
import tempfile
import textwrap
import unittest
from pathlib import Path

from src.integrations.codex_skills import SkillCatalog, build_public_skill_profile, parse_skill_file
from src.integrations.codex_skills.skill_capabilities import build_skill_capability_registry


class PublicSkillProfileTest(unittest.TestCase):
    def test_profile_uses_public_usage_and_never_raw_body_or_runtime_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            skill_dir = root / "demo"
            (skill_dir / "scripts").mkdir(parents=True)
            skill_file = skill_dir / "SKILL.md"
            skill_file.write_text(
                textwrap.dedent(
                    """\
                    ---
                    name: demo-skill
                    capability_id: skill.demo
                    display_name: 演示 Skill
                    description: 面向用户的演示能力。
                    triggers: [演示]
                    public_usage:
                      overview: 解释可用输入格式和示例。
                      input_formats:
                        - name: demo_data
                          description: CSV 表格，必须包含 id。
                          internal_path: scripts/run_demo.py
                      examples:
                        - /demo 用这个 CSV 处理
                    execution:
                      mode: python_subprocess
                      handler: internal.handler
                    parameters:
                      demo_data:
                        type: artifact
                        required: true
                        source: artifact
                    scripts:
                      - name: run_demo
                        path: scripts/run_demo.py
                        runtime: python
                    ---

                    # Internal body

                    scripts/run_demo.py and Rscript wrapper implementation details.
                    """
                ),
                encoding="utf-8",
            )
            manifest = parse_skill_file(skill_file)
            profile = build_public_skill_profile(manifest, capability_id="skill.demo").to_dict()

        payload = json.dumps(profile, ensure_ascii=False, sort_keys=True)
        self.assertIn("解释可用输入格式", payload)
        self.assertIn("demo_data", payload)
        for forbidden in ("scripts/run_demo.py", "Rscript", "wrapper", "handler", "internal_path"):
            self.assertNotIn(forbidden, payload)

    def test_all_project_skill_profiles_are_public_and_no_leak(self) -> None:
        root = Path("skill")
        catalog = SkillCatalog.from_roots((root,))
        registry = build_skill_capability_registry(
            catalog,
            public_skill_roots=(root,),
            reserved_capability_ids=("main_agent.respond",),
        )
        self.assertGreater(len(registry.descriptors_by_id), 0)

        forbidden = (
            "source_path",
            "scripts/",
            "Rscript",
            "wrapper",
            "platform_service",
            "handler",
            "sidecar",
            "config.yaml",
            "mysql://",
            "postgresql://",
            "token",
            "secret",
            "api_key",
        )
        for capability_id, skill_name in registry.skill_name_by_capability_id.items():
            with self.subTest(capability_id=capability_id):
                manifest = catalog.get(skill_name)
                self.assertIsNotNone(manifest)
                public_usage = manifest.metadata.get("public_usage") if manifest is not None else None
                self.assertIsInstance(public_usage, dict)
                profile = build_public_skill_profile(
                    manifest,
                    capability_id=capability_id,
                    descriptor=registry.descriptors_by_id.get(capability_id),
                ).to_dict()
                payload = json.dumps(profile, ensure_ascii=False, sort_keys=True)
                self.assertIn("public_usage", profile)
                self.assertTrue(profile["public_usage"])
                for token in forbidden:
                    self.assertNotIn(token, payload)


if __name__ == "__main__":
    unittest.main()
