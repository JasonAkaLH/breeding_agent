from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.integrations.agent_skills import parse_input_schema_file


class InputSchemaParserTest(unittest.TestCase):
    def test_parses_supported_field_types(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "demo.input.yaml"
            path.write_text(
                """
schema_version: '1'
schema_id: demo
title: Demo
inputs:
  s: {type: string, required: true, aliases: [名称]}
  i: {type: integer, validation: {min: 1, max: 10}}
  n: {type: number}
  b: {type: boolean}
  o: {type: object}
  a: {type: array}
  f: {type: artifact, source: {allowed: [artifact]}}
constraints:
  - any_of: [f, s]
slot_policy: {max_rounds: 3}
entrypoint_mapping: run
""",
                encoding="utf-8",
            )
            schema = parse_input_schema_file(path)

        self.assertEqual(schema.schema_id, "demo")
        self.assertEqual(set(schema.inputs), {"s", "i", "n", "b", "o", "a", "f"})
        self.assertTrue(schema.inputs["s"].required)
        self.assertEqual(schema.inputs["f"].source.allowed, ("artifact",))
        self.assertEqual(schema.constraints[0]["any_of"], ["f", "s"])
        self.assertEqual(schema.entrypoint_mapping, "run")
