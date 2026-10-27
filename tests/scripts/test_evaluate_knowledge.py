from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import AsyncMock, patch

from scripts.evaluate_knowledge import EvaluationFailure, _MeasuredModel, load_manifest, main


class EvaluateKnowledgeTest(unittest.TestCase):
    def test_mixed_format_manifest_and_empty_library_are_accepted(self):
        from tests.capabilities.knowledge.pdf_fixtures import text_pdf

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path, manifest = self._manifest(root)
            ((root / "paper.PDF").resolve()).write_bytes(text_pdf("Full evidence"))
            manifest["libraries"][0]["documents"].append({"id": "pdf1", "path": "paper.PDF"})
            manifest["libraries"].append({"id": "empty", "name": "Empty", "documents": []})
            path.write_text(json.dumps(manifest))
            loaded = load_manifest(path)
            self.assertEqual(loaded["libraries"][0]["documents"][1]["resolved_path"], (root / "paper.PDF").resolve())

    def test_empty_library_answer_does_not_require_a_fabricated_citation(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, _ = self._manifest(Path(temporary))
            result = {"status": "completed", "documents_read": 0, "citation_marker_present": False, "citation_format_valid": True,
                      **dict.fromkeys(("expected_documents_read", "expected_documents_completely_read", "expected_facts_present", "expected_bibliographic_titles_present", "required_compaction_observed"), True)}
            with patch("scripts.evaluate_knowledge.evaluate", new_callable=AsyncMock, return_value={"results": [result]}), redirect_stdout(StringIO()):
                self.assertEqual(main(["--config", "never-read.yaml", "--corpus", str(path), "--live"]), 0)

    def test_marker_alone_does_not_pass_citation_acceptance(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, _ = self._manifest(Path(temporary))
            result = {"status": "completed", "documents_read": 1, "citation_marker_present": True,
                      "citation_format_valid": False,
                      **dict.fromkeys(("expected_documents_read", "expected_documents_completely_read", "expected_facts_present", "expected_bibliographic_titles_present", "required_compaction_observed"), True)}
            with patch("scripts.evaluate_knowledge.evaluate", new_callable=AsyncMock, return_value={"results": [result]}), redirect_stdout(StringIO()):
                self.assertEqual(main(["--config", "never-read.yaml", "--corpus", str(path), "--live"]), 2)

    def _manifest(self, root: Path):
        (root / "private-literature.md").write_text("PRIVATE FULL TEXT", encoding="utf-8")
        manifest = {
            "libraries": [{"id": "rice", "name": "Private name", "documents": [{"id": "paper1", "path": "private-literature.md"}]}],
            "questions": [{"id": "q1", "text": "PRIVATE QUESTION", "expected_document_ids": ["paper1"], "expected_facts": ["PRIVATE FACT"]}],
        }
        path = root / "manifest.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        return path, manifest

    def test_dry_run_validates_corpus_without_provider_call_or_private_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, _ = self._manifest(Path(temporary))
            output = StringIO()
            with patch("scripts.evaluate_knowledge.evaluate", new_callable=AsyncMock) as evaluate, redirect_stdout(output):
                result = main(["--config", "never-read-config.yaml", "--corpus", str(path)])
            evaluate.assert_not_called()
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue()), {"live": False, "libraries": 1, "questions": 1})
        self.assertNotIn("PRIVATE", output.getvalue())
        self.assertNotIn(temporary, output.getvalue())

    def test_paths_outside_explicit_corpus_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, manifest = self._manifest(Path(temporary))
            manifest["libraries"][0]["documents"][0]["path"] = "../outside.md"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "knowledge_evaluation_document_invalid"):
                load_manifest(path)

    def test_duplicate_ids_and_unknown_expected_documents_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, manifest = self._manifest(Path(temporary))
            manifest["questions"][0]["expected_document_ids"] = ["unknown"]
            path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "knowledge_evaluation_reference_invalid"):
                load_manifest(path)
            manifest["questions"][0]["expected_document_ids"] = []
            manifest["libraries"].append(manifest["libraries"][0])
            path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "knowledge_evaluation_identifier_invalid"):
                load_manifest(path)

    def test_live_provider_failure_prints_only_safe_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, _ = self._manifest(Path(temporary))
            output = StringIO()
            with redirect_stderr(output), patch("scripts.evaluate_knowledge.evaluate", new_callable=AsyncMock, side_effect=ValueError("PRIVATE RESPONSE AND CREDENTIAL")):
                result = main(["--config", "private-config.yaml", "--corpus", str(path), "--live"])
        self.assertEqual(result, 2)
        self.assertEqual(json.loads(output.getvalue()), {"error": "knowledge_evaluation_failed", "private_details_omitted": True})
        self.assertNotIn("PRIVATE", output.getvalue())
        self.assertNotIn(temporary, output.getvalue())

    def test_ingestion_failure_has_closed_phase_and_code(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, _ = self._manifest(Path(temporary))
            output = StringIO()
            with redirect_stderr(output), patch("scripts.evaluate_knowledge.evaluate", new_callable=AsyncMock, side_effect=EvaluationFailure("ingestion", "provider_call_failed")):
                self.assertEqual(main(["--config", "never-read.yaml", "--corpus", str(path), "--live"]), 2)
            self.assertEqual(json.loads(output.getvalue())["code"], "provider_call_failed")
            self.assertNotIn(temporary, output.getvalue())


class MeasuredModelTest(unittest.IsolatedAsyncioTestCase):
    async def test_failed_attempt_is_counted_without_response_or_error_text(self):
        from types import SimpleNamespace

        measured = _MeasuredModel(SimpleNamespace(sample_agent=AsyncMock(side_effect=ValueError("PRIVATE RESPONSE"))))
        with self.assertRaises(ValueError):
            await measured.sample_agent(SimpleNamespace(request_id="sample:knowledge-answer-1"))
        self.assertEqual(len(measured.calls), 1)
        self.assertTrue(measured.calls[0]["citation_repair"])
        self.assertEqual(measured.calls[0]["status"], "failed")
        self.assertEqual(measured.calls[0]["usage_status"], "unavailable")
        self.assertNotIn("PRIVATE", json.dumps(measured.calls))


if __name__ == "__main__":
    unittest.main()
