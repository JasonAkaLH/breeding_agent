from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock
import zipfile
from pathlib import Path

from src.integrations.agent_skills import SkillScriptRunner, parse_skill_file
from src.integrations.agent_skills.output_files import collect_skill_output_files, create_zip_from_collected_files


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


    def test_collects_xlsx_with_runtime_stable_mime(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            outputs = Path(tmpdir) / "outputs"
            outputs.mkdir()
            (outputs / "fieldbook.xlsx").write_bytes(b"PK\x03\x04fake-xlsx")
            with mock.patch("src.integrations.agent_skills.output_files.mimetypes.guess_type", return_value=("application/octet-stream", None)):
                result = collect_skill_output_files(
                    {
                        "output_files": [
                            {
                                "path": "outputs/fieldbook.xlsx",
                                "filename": "fieldbook.xlsx",
                                "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            }
                        ]
                    },
                    outputs,
                )

        self.assertEqual(result.rejections, ())
        self.assertEqual(len(result.files), 1)
        self.assertEqual(result.files[0].mime_type, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    def test_v2_contract_artifact_constraints_are_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            manifest = self._write_v2_manifest(root, artifacts="""
      - extensions: [.html]
        mime_types: [text/html]
""")
            outputs = root / "outputs"
            outputs.mkdir()
            (outputs / "notes.csv").write_text("a,b\n", encoding="utf-8")

            result = collect_skill_output_files({"output_files": [{"path": "outputs/notes.csv"}]}, outputs, manifest=manifest)

        self.assertEqual(result.files, ())
        self.assertEqual([item.reason for item in result.rejections], ["manifest_extension_not_allowed"])

    def test_v2_contract_artifact_constraints_allow_declared_xlsx(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            manifest = self._write_v2_manifest(root, artifacts="""
      - extensions: [.xlsx]
        mime_types: [application/vnd.openxmlformats-officedocument.spreadsheetml.sheet]
""")
            outputs = root / "outputs"
            outputs.mkdir()
            (outputs / "fieldbook.xlsx").write_bytes(b"PK\x03\x04fake-xlsx")

            result = collect_skill_output_files(
                {"output_files": [{"path": "outputs/fieldbook.xlsx", "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}]},
                outputs,
                manifest=manifest,
            )

        self.assertEqual(result.rejections, ())
        self.assertEqual(len(result.files), 1)
        self.assertEqual(result.files[0].filename, "fieldbook.xlsx")


    def test_v2_contract_artifact_constraints_match_compound_extensions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            manifest = self._write_v2_manifest(root, artifacts="""
      - extensions: [.vcf.gz]
        mime_types: [application/gzip]
""")
            outputs = root / "outputs"
            outputs.mkdir()
            (outputs / "sample.vcf.gz").write_bytes(b"fake-gzip")

            result = collect_skill_output_files(
                {"output_files": [{"path": "outputs/sample.vcf.gz", "mime_type": "application/gzip"}]},
                outputs,
                manifest=manifest,
            )

        self.assertEqual(result.rejections, ())
        self.assertEqual(len(result.files), 1)
        self.assertEqual(result.files[0].filename, "sample.vcf.gz")

    def _write_v2_manifest(self, root: Path, *, artifacts: str):
        skill = root / "SKILL.md"
        skill.write_text(
            """---
name: file-skill
description: file skill
triggers: [file]
---
File skill.
""",
            encoding="utf-8",
        )
        (root / "skill.contract.yaml").write_text(
            f"""contract_version: '2'
capability:
  id: skill.file
  display_name: File Skill
runtime:
  mode: python_subprocess
entrypoints:
  run:
    path: scripts/run.py
    output: file_output
outputs:
  file_output:
    required: [answer]
    artifacts:
{artifacts}
""",
            encoding="utf-8",
        )
        return parse_skill_file(skill)

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
