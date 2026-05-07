from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from src.api.runtime import _default_skill_roots


class DefaultSkillRootsTest(unittest.TestCase):
    def test_project_skill_root_uses_repo_skill_directory(self) -> None:
        original_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmpdir:
            try:
                os.chdir(tmpdir)
                project_root = Path.cwd()
                roots = _default_skill_roots()
            finally:
                os.chdir(original_cwd)

        self.assertEqual(roots[0], project_root / "skill")
        self.assertEqual(roots[1], Path.home() / ".codex" / "skills")


if __name__ == "__main__":
    unittest.main()
