from __future__ import annotations

import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path

from src.integrations.codex_skills import SkillScriptRunner, parse_skill_file
from src.integrations.codex_skills.output_files import collect_skill_output_files, create_zip_from_collected_files


class SkillOutputFileCollectionTest(unittest.TestCase):
    def test_runner_exposes_output_dir_to_script_and_processor(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            skill = root / "SKILL.md"
            script = root / "scripts" / "emit.py"
            script.parent.mkdir()
            skill.write_text(
                """---
name: file-skill
description: file skill
triggers: [file]
scripts:
  - name: emit
    path: scripts/emit.py
    runtime: python
    auto_run: true
outputs:
  required: [answer]
---
File skill.
""",
                encoding="utf-8",
            )
            script.write_text(
                """import json, os
from pathlib import Path
out = Path(os.environ['MAF_SKILL_OUTPUT_DIR'])
out.mkdir(parents=True, exist_ok=True)
(out / 'report.html').write_text('<h1>ok</h1>', encoding='utf-8')
print(json.dumps({'answer': 'done', 'output_files': [{'path': 'outputs/report.html', 'mime_type': 'text/html'}]}))
""",
                encoding="utf-8",
            )
            manifest = parse_skill_file(skill)
            seen = {}

            async def processor(*, output, outputs_dir, manifest, script, context):
                seen["exists"] = outputs_dir.exists()
                seen["file_text"] = (outputs_dir / "report.html").read_text(encoding="utf-8")
                return {**output, "managed": True}

            runner = SkillScriptRunner(output_processor=processor)
            output = __import__("asyncio").run(runner.run(manifest, manifest.scripts[0], {"query": "file"}))

        self.assertTrue(seen["exists"])
        self.assertEqual(seen["file_text"], "<h1>ok</h1>")
        self.assertTrue(output["managed"])

    def test_collects_only_safe_allowed_relative_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            outputs = Path(tmpdir) / "outputs"
            outputs.mkdir()
            (outputs / "report.html").write_text("ok", encoding="utf-8")
            (outputs / "notes.csv").write_text("a,b\n", encoding="utf-8")
            result = collect_skill_output_files(
                {
                    "output_files": [
                        {"path": "outputs/report.html", "label": "布局", "summary": "html"},
                        {"path": "outputs/notes.csv", "filename": "notes.csv", "mime_type": "text/csv"},
                        {"path": "../secret.txt"},
                    ]
                },
                outputs,
            )

        self.assertEqual([file.archive_name for file in result.files], ["report.html", "notes.csv"])
        self.assertTrue(any(item.reason == "unsafe_path" for item in result.rejections))
        self.assertEqual(result.files[0].mime_type, "text/html")
        self.assertEqual(result.files[0].label, "布局")

    def test_rejects_source_zip_but_allows_platform_generated_zip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            outputs = root / "outputs"
            outputs.mkdir()
            (outputs / "a.html").write_text("A", encoding="utf-8")
            (outputs / "b.csv").write_text("B", encoding="utf-8")
            (outputs / "source.zip").write_bytes(b"zip")
            result = collect_skill_output_files(
                {"output_files": [{"path": "outputs/a.html"}, {"path": "outputs/b.csv"}, {"path": "outputs/source.zip"}]},
                outputs,
            )
            zip_path = root / "bundle.zip"
            bundle = create_zip_from_collected_files(result.files, zip_path)
            with zipfile.ZipFile(zip_path) as archive:
                names = sorted(archive.namelist())

        self.assertEqual(names, ["a.html", "b.csv"])
        self.assertEqual(bundle.source_file_count, 2)
        self.assertTrue(any(item.reason == "extension_not_allowed" for item in result.rejections))

    def test_rejects_hardlinked_output_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            outside = root / "outside.txt"
            outside.write_text("secret", encoding="utf-8")
            outputs = root / "outputs"
            outputs.mkdir()
            hardlink = outputs / "hardlink.txt"
            os.link(outside, hardlink)

            result = collect_skill_output_files({"output_files": [{"path": "outputs/hardlink.txt"}]}, outputs)

        self.assertEqual(result.files, ())
        self.assertTrue(any(item.reason == "hardlink_not_allowed" for item in result.rejections))

    def test_rejects_declared_mime_that_does_not_match_extension(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            outputs = Path(tmpdir) / "outputs"
            outputs.mkdir()
            (outputs / "report.html").write_text("<h1>ok</h1>", encoding="utf-8")

            result = collect_skill_output_files(
                {"output_files": [{"path": "outputs/report.html", "mime_type": "application/json"}]},
                outputs,
            )

        self.assertEqual(result.files, ())
        self.assertTrue(any(item.reason == "mime_mismatch" for item in result.rejections))


if __name__ == "__main__":
    unittest.main()
