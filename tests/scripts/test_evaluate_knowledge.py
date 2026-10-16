from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import AsyncMock, patch

from scripts.evaluate_knowledge import load_manifest, main


class EvaluateKnowledgeTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
