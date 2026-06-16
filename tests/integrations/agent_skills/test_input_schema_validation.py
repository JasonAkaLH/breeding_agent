from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.integrations.agent_skills import parse_input_schema_file, validate_selected_schema_payload


def _schema(text: str):
    tmp = tempfile.TemporaryDirectory()
    path = Path(tmp.name) / "schema.input.yaml"
    path.write_text(text, encoding="utf-8")
    return parse_input_schema_file(path)


class InputSchemaValidationTest(unittest.TestCase):
    def test_field_design_diagonal_and_interval_require_ncols_from_schema(self) -> None:
        diagonal = parse_input_schema_file("skill/field-design/schemas/diagonal.input.yaml")
        interval = parse_input_schema_file("skill/field-design/schemas/interval.input.yaml")

        self.assertTrue(diagonal.inputs["ncols"].required)
        self.assertTrue(interval.inputs["ncols"].required)
        self.assertIn("对角线增广", diagonal.inputs["design"].aliases)
        self.assertEqual(set(validate_selected_schema_payload(diagonal, {"design": "diagonal"}).missing), {"material_data", "ncols"})
        self.assertEqual(
            set(validate_selected_schema_payload(interval, {"design": "interval", "ck_spec": "1,1,10"}).missing),
            {"material_data", "ncols"},
        )

    def test_required_is_scoped_to_selected_schema(self) -> None:
        rcbd = _schema("""
schema_id: rcbd
inputs:
  design: {type: string, required: true, const: rcbd}
  ncols: {type: integer, required: true}
""")
        interval = _schema("""
schema_id: interval
inputs:
  design: {type: string, required: true, const: interval}
  ncols: {type: integer, required: true}
  ck_spec: {type: string, required: true}
""")

        self.assertEqual(validate_selected_schema_payload(rcbd, {"design": "rcbd", "ncols": 10}).missing, ())
        self.assertEqual(validate_selected_schema_payload(interval, {"design": "interval", "ncols": 10}).missing, ("ck_spec",))

    def test_artifact_source_policy_rejects_text_candidate(self) -> None:
        schema = _schema("""
schema_id: artifact_demo
inputs:
  upload: {type: artifact, required: true, source: {allowed: [artifact]}}
""")
        result = validate_selected_schema_payload(schema, {"upload": {"filename": "x.csv"}}, candidate_sources={"upload": "llm"})
        self.assertEqual(result.invalid[0].reason, "artifact_source_denied")


    def test_artifact_file_extensions_validate_concrete_runtime_metadata(self) -> None:
        schema = _schema("""
schema_id: artifact_ext
inputs:
  upload:
    type: artifact
    required: true
    source: {allowed: [artifact]}
    validation:
      file_extensions: [.csv, .vcf.gz]
""")

        csv_result = validate_selected_schema_payload(
            schema,
            {"upload": {"filename": "materials.csv"}},
            candidate_sources={"upload": "artifact"},
        )
        compound_result = validate_selected_schema_payload(
            schema,
            {"upload": {"filename": "sample.vcf.gz"}},
            candidate_sources={"upload": "artifact"},
        )
        plain_gz_result = validate_selected_schema_payload(
            schema,
            {"upload": {"filename": "archive.gz"}},
            candidate_sources={"upload": "artifact"},
        )
        pdf_result = validate_selected_schema_payload(
            schema,
            {"upload": {"filename": "report.pdf"}},
            candidate_sources={"upload": "artifact"},
        )

        self.assertTrue(csv_result.ok)
        self.assertTrue(compound_result.ok)
        self.assertEqual([issue.reason for issue in plain_gz_result.invalid], ["file_extension"])
        self.assertEqual([issue.reason for issue in pdf_result.invalid], ["file_extension"])

    def test_artifact_file_extensions_keep_legacy_available_placeholder_compatible(self) -> None:
        schema = _schema("""
schema_id: artifact_placeholder
inputs:
  upload:
    type: artifact
    required: true
    source: {allowed: [artifact]}
    validation:
      file_extensions: [.csv]
""")

        result = validate_selected_schema_payload(
            schema,
            {"upload": {"available": True, "count": 1}},
            candidate_sources={"upload": "artifact"},
        )

        self.assertTrue(result.ok)

    def test_basic_validation_rules(self) -> None:
        schema = _schema("""
schema_id: rules
inputs:
  code: {type: string, required: true, validation: {regex: '^[A-Z]{2}$', min_length: 2, max_length: 2}}
  count: {type: integer, validation: {min: 1, max: 5}}
""")
        result = validate_selected_schema_payload(schema, {"code": "abc", "count": 9})
        self.assertEqual([issue.reason for issue in result.invalid], ["regex", "max"])

    def test_constraints(self) -> None:
        schema = _schema("""
schema_id: ocr
inputs:
  document: {type: artifact, source: {allowed: [artifact]}}
  file_path: {type: string}
  output_format: {type: string, enum: [text, markdown, json]}
  sidecar: {type: string}
constraints:
  - any_of: [document, file_path]
  - mutually_exclusive: [document, file_path]
  - dependencies: {sidecar: [file_path]}
""")
        missing = validate_selected_schema_payload(schema, {"output_format": "pdf"})
        self.assertIn("document", missing.missing)
        self.assertIn("enum", [issue.reason for issue in missing.invalid])
        exclusive = validate_selected_schema_payload(schema, {"document": {"filename": "a.pdf"}, "file_path": "/tmp/a.pdf"}, candidate_sources={"document": "artifact"})
        self.assertIn("mutually_exclusive", [issue.reason for issue in exclusive.invalid])
        deps = validate_selected_schema_payload(schema, {"sidecar": "x"})
        self.assertIn("file_path", deps.missing)
