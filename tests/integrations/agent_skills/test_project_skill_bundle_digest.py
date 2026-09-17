from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from src.integrations.agent_skills.bundle_digest import (
    ProjectSkillBundleDigestError,
    _encode_relative_path,
    compute_project_skill_bundle_digest,
    validate_project_skill_bundle_digest,
)


class _AdvancingClock:
    def __init__(self, *values: float) -> None:
        self._values = iter(values)
        self._last = 0.0

    def __call__(self) -> float:
        self._last = next(self._values, self._last)
        return self._last


class ProjectSkillBundleDigestTest(unittest.TestCase):
    def test_empty_bundle_matches_sha256_empty_vector(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            result = compute_project_skill_bundle_digest(Path(temp_dir))

        self.assertEqual(result.digest, "sha256:" + hashlib.sha256(b"").hexdigest())
        self.assertEqual(result.file_count, 0)
        self.assertEqual(result.total_bytes, 0)

    def test_digest_is_deterministic_and_ignores_only_closed_cache_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "b").mkdir()
            (root / "b" / "two.txt").write_text("two", encoding="utf-8")
            (root / "a.txt").write_text("one", encoding="utf-8")
            first = compute_project_skill_bundle_digest(root)

            (root / "__pycache__").mkdir()
            (root / "__pycache__" / "ignored.pyc").write_bytes(b"cache")
            (root / "also-ignored.pyc").write_bytes(b"cache")
            (root / ".git").mkdir()
            (root / ".git" / "index").write_bytes(b"git")
            second = compute_project_skill_bundle_digest(root)

            self.assertEqual(first.digest, second.digest)
            self.assertEqual(second.file_count, 2)
            (root / ".DS_Store").write_bytes(b"not-ignored")
            third = compute_project_skill_bundle_digest(root)

        self.assertNotEqual(second.digest, third.digest)
        self.assertEqual(third.file_count, 3)

    def test_content_path_and_size_each_change_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            original = root / "skill.txt"
            original.write_text("alpha", encoding="utf-8")
            first = compute_project_skill_bundle_digest(root)
            original.write_text("bravo", encoding="utf-8")
            content_changed = compute_project_skill_bundle_digest(root)
            renamed = root / "renamed.txt"
            original.rename(renamed)
            path_changed = compute_project_skill_bundle_digest(root)
            renamed.write_text("bravo-longer", encoding="utf-8")
            size_changed = compute_project_skill_bundle_digest(root)

        self.assertNotEqual(first.digest, content_changed.digest)
        self.assertNotEqual(content_changed.digest, path_changed.digest)
        self.assertNotEqual(path_changed.digest, size_changed.digest)

    def test_root_symlink_is_allowed_but_nested_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            parent = Path(temp_dir)
            root = parent / "bundle"
            root.mkdir()
            (root / "SKILL.md").write_text("skill", encoding="utf-8")
            alias = parent / "alias"
            alias.symlink_to(root, target_is_directory=True)
            self.assertEqual(
                compute_project_skill_bundle_digest(alias).digest,
                compute_project_skill_bundle_digest(root).digest,
            )
            (root / "escape").symlink_to(parent / "outside")
            with self.assertRaises(ProjectSkillBundleDigestError) as captured:
                compute_project_skill_bundle_digest(root)

        self.assertEqual(captured.exception.code, "project_skill_bundle_unsafe_entry")
        self.assertEqual(captured.exception.reason, "symlink")

    def test_special_file_and_non_utf8_path_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            fifo = root / "pipe"
            os.mkfifo(fifo)
            with self.assertRaises(ProjectSkillBundleDigestError) as special:
                compute_project_skill_bundle_digest(root)
            self.assertEqual(special.exception.reason, "special_file")
            fifo.unlink()

            with self.assertRaises(ProjectSkillBundleDigestError) as non_utf8:
                _encode_relative_path("bad-\udcff")

        self.assertEqual(non_utf8.exception.reason, "non_utf8_path")

    def test_file_byte_and_deadline_limits_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "one").write_bytes(b"1")
            (root / "two").write_bytes(b"22")
            with self.assertRaises(ProjectSkillBundleDigestError) as files:
                compute_project_skill_bundle_digest(root, max_files=1)
            with self.assertRaises(ProjectSkillBundleDigestError) as size:
                compute_project_skill_bundle_digest(root, max_total_bytes=2)
            with self.assertRaises(ProjectSkillBundleDigestError) as deadline:
                compute_project_skill_bundle_digest(
                    root,
                    deadline_seconds=1.0,
                    monotonic=_AdvancingClock(0.0, 0.0, 2.0),
                )

        self.assertEqual(files.exception.reason, "file_limit")
        self.assertEqual(size.exception.reason, "byte_limit")
        self.assertEqual(deadline.exception.reason, "deadline")

    def test_expected_digest_format_and_mismatch_are_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            actual = compute_project_skill_bundle_digest(root)
            validated = validate_project_skill_bundle_digest(root, actual.digest)
            self.assertEqual(validated.digest, actual.digest)
            self.assertEqual(validated.file_count, actual.file_count)
            self.assertEqual(validated.total_bytes, actual.total_bytes)
            with self.assertRaises(ProjectSkillBundleDigestError) as invalid:
                validate_project_skill_bundle_digest(root, "SHA256:bad")
            with self.assertRaises(ProjectSkillBundleDigestError) as mismatch:
                validate_project_skill_bundle_digest(root, "sha256:" + "0" * 64)

        self.assertEqual(invalid.exception.code, "project_skill_bundle_digest_invalid")
        self.assertEqual(mismatch.exception.code, "project_skill_bundle_digest_mismatch")


if __name__ == "__main__":
    unittest.main()
