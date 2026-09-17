from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.capabilities.main_agent.prompt_builder import build_tool_input_schemas_from_profiles
from src.integrations.agent_skills import SkillCatalog, build_public_skill_profile
from src.integrations.agent_skills.skill_capabilities import build_skill_capability_registry


class PublicSkillProfileTest(unittest.TestCase):
    def _write_v2_skill(self, root: Path) -> Path:
        skill_dir = root / "demo"
        (skill_dir / "scripts").mkdir(parents=True)
        (skill_dir / "schemas").mkdir()
        (skill_dir / "references").mkdir()
        (skill_dir / "SKILL.md").write_text(
            """---
name: demo-skill
description: 面向用户的演示能力。
---

# Demo

Use resources by id; do not expose scripts/run.py.
""",
            encoding="utf-8",
        )
        (skill_dir / "skill.contract.yaml").write_text(
            """
contract_version: '2'
capability:
  id: skill.demo
  display_name: 演示 Skill
  description: 面向用户的演示能力。
routing:
  triggers: [演示]
  examples: [/demo 用这个 CSV 处理]
file_selection:
  required: true
  supported_file_types: [csv]
  expected_content: [材料表]
runtime:
  mode: python_subprocess
  answer_mode: direct
entrypoints:
  run:
    path: scripts/run.py
    input_schema: demo_input
    output: demo_output
input_schemas:
  demo_input:
    path: schemas/demo.input.yaml
    title: Demo input
    description: 用户可见输入摘要
    aliases: [材料表]
outputs:
  demo_output:
    required: [response_text]
    artifacts:
      - extensions: [.json]
        mime_types: [application/json]
        storage_key: must-not-leak
resources:
  usage:
    path: references/usage.md
    title: 用法说明
    description: 公开使用说明
    audience: [main_agent, slot_question]
""",
            encoding="utf-8",
        )
        (skill_dir / "schemas" / "demo.input.yaml").write_text(
            """
schema_id: demo_input
inputs:
  material_file:
    type: artifact
    required: true
    description: 材料文件
    file_selection:
      required: true
      expected_content: [材料表]
      supported_file_types: [csv]
      helpful_columns: [ped_id]
      disambiguation_hint: 优先选择材料表。
  mode:
    type: string
    title: 模式
    description: 处理模式
    aliases: [运行模式]
    default: summary
    enum: [summary, full]
    question: 请选择处理模式。
    clarification:
      examples: [summary]
  max_results:
    type: integer
    title: 最大结果数
    description: 最多返回多少条结果。
    required_when: {mode: full}
    default: 30
    validation: {min: 1, max: 100, regex: must-not-leak}
    source: {allowed: [llm]}
    patterns: ['\\d+']
  internal_token:
    type: string
    expose: false
    default: secret-value
constraints:
  - any_of: [material_file, mode]
  - dependencies: {max_results: [mode]}
slot_policy: {max_rounds: 3}
entrypoint_mapping: run
""",
            encoding="utf-8",
        )
        return skill_dir

    def test_v2_profile_uses_contract_resource_index_and_schema_summaries(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._write_v2_skill(root)
            catalog = SkillCatalog.from_roots((root,))
            manifest = catalog.get("demo-skill")
            assert manifest is not None
            profile = build_public_skill_profile(manifest, capability_id="skill.demo").to_dict()

        payload = json.dumps(profile, ensure_ascii=False, sort_keys=True)
        self.assertEqual(profile["capability_id"], "skill.demo")
        self.assertEqual(profile["display_name"], "演示 Skill")
        self.assertEqual(profile["resource_index"][0]["resource_id"], "usage")
        self.assertEqual(profile["schema_summaries"][0]["schema_id"], "demo_input")
        fields = profile["schema_summaries"][0]["fields"]
        self.assertEqual([field["name"] for field in fields], ["material_file", "max_results", "mode"])
        max_results = fields[1]
        self.assertEqual(max_results["default"], 30)
        self.assertEqual(max_results["required_when"], {"mode": "full"})
        self.assertEqual(max_results["validation"], {"max": 100.0, "min": 1.0})
        self.assertEqual(fields[2]["clarification"], {"examples": ["summary"]})
        self.assertNotIn("internal_token", payload)
        self.assertNotIn('"source":', payload)
        self.assertNotIn("patterns", payload)
        self.assertNotIn("regex", payload)
        self.assertNotIn("slot_policy", payload)
        self.assertNotIn("entrypoint_mapping", payload)
        self.assertEqual(
            profile["schema_summaries"][0]["constraints"],
            [
                {"any_of": ["material_file", "mode"]},
                {"dependencies": {"max_results": ["mode"]}},
            ],
        )
        self.assertEqual(
            profile["outputs"],
            {
                "output_contracts": [
                    {
                        "artifacts": [
                            {
                                "extensions": [".json"],
                                "mime_types": ["application/json"],
                            }
                        ],
                        "output_id": "demo_output",
                        "required_fields": ["response_text"],
                    }
                ]
            },
        )
        self.assertTrue(profile["file_selection"]["required"])
        self.assertEqual(profile["file_selection_summaries"][0]["field"], "material_file")
        tool_schemas = build_tool_input_schemas_from_profiles([profile])
        self.assertTrue(tool_schemas[0]["file_selection"]["required"])
        self.assertEqual(tool_schemas[0]["file_selection_summaries"][0]["field"], "material_file")
        self.assertNotIn("file_intent", profile)
        self.assertNotIn("file_intent", tool_schemas[0])
        self.assertIn("材料表", payload)
        for forbidden in ("scripts/run.py", "python_subprocess", "handler", "runtime", "config.yaml", "token", "secret", "path"):
            self.assertNotIn(forbidden, payload)

    def test_registry_profile_for_valid_v2_skill_has_no_internal_leak(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._write_v2_skill(root)
            catalog = SkillCatalog.from_roots((root,))
            registry = build_skill_capability_registry(catalog, public_skill_roots=(root,))
            self.assertEqual(set(registry.descriptors_by_id), {"skill.demo"})
            manifest = catalog.get("demo-skill")
            assert manifest is not None
            profile = build_public_skill_profile(manifest, capability_id="skill.demo", descriptor=registry.descriptors_by_id["skill.demo"]).to_dict()

        serialized = json.dumps(profile, ensure_ascii=False, sort_keys=True)
        self.assertIn("resource_index", profile)
        self.assertIn("schema_summaries", profile)
        self.assertNotIn("scripts/", serialized)
        self.assertNotIn("handler", serialized)
        self.assertNotIn("platform_service", serialized)

    def test_unknown_constraint_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            skill_dir = self._write_v2_skill(root)
            schema_path = skill_dir / "schemas" / "demo.input.yaml"
            schema_path.write_text(
                schema_path.read_text(encoding="utf-8")
                + "\nconstraints:\n  - unsupported_shape: [mode]\n",
                encoding="utf-8",
            )
            catalog = SkillCatalog.from_roots((root,))
            manifest = catalog.get("demo-skill")
            assert manifest is not None

            with self.assertRaisesRegex(ValueError, "constraint"):
                build_public_skill_profile(manifest, capability_id="skill.demo")

    def test_forbidden_strict_json_default_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            skill_dir = self._write_v2_skill(root)
            schema_path = skill_dir / "schemas" / "demo.input.yaml"
            schema_path.write_text(
                schema_path.read_text(encoding="utf-8").replace(
                    "default: 30",
                    "default: {storage_key: private}",
                ),
                encoding="utf-8",
            )
            catalog = SkillCatalog.from_roots((root,))
            manifest = catalog.get("demo-skill")
            assert manifest is not None

            with self.assertRaisesRegex(ValueError, "forbidden"):
                build_public_skill_profile(manifest, capability_id="skill.demo")


if __name__ == "__main__":
    unittest.main()
