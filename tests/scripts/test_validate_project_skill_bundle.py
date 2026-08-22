from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from scripts.validate_project_skill_bundle import main


class ValidateProjectSkillBundleScriptTest(unittest.TestCase):
    def test_direct_script_entrypoint_resolves_repository_imports(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            completed = subprocess.run(
                [
                    sys.executable,
                    str(Path("scripts/validate_project_skill_bundle.py").resolve()),
                    "--root",
                    temp_dir,
                ],
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["status"], "reported")

    def test_report_and_expected_validation_are_safe_and_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "SKILL.md").write_text("safe", encoding="utf-8")
            output = StringIO()
            with redirect_stdout(output):
                exit_code = main(["--root", str(root)])
            report = json.loads(output.getvalue())
            expected = report["digest"]
            validated = StringIO()
            with redirect_stdout(validated):
                validated_exit = main(
                    ["--root", str(root), "--expected", expected]
                )

        self.assertEqual(exit_code, 0)
        self.assertEqual(validated_exit, 0)
        self.assertEqual(json.loads(validated.getvalue())["status"], "valid")
        self.assertEqual(set(report), {"digest", "duration_ms", "file_count", "status", "total_bytes"})
        self.assertNotIn(str(root), output.getvalue())

    def test_failure_prints_only_closed_safe_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "secret-name.txt").write_text("secret-body", encoding="utf-8")
            output = StringIO()
            with redirect_stdout(output):
                exit_code = main(
                    ["--root", str(root), "--expected", "sha256:" + "0" * 64]
                )
            report = json.loads(output.getvalue())

        self.assertEqual(exit_code, 2)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["code"], "project_skill_bundle_digest_mismatch")
        self.assertNotIn("secret-name", output.getvalue())
        self.assertNotIn("secret-body", output.getvalue())
        self.assertNotIn(str(root), output.getvalue())


if __name__ == "__main__":
    unittest.main()
