from __future__ import annotations

import tempfile
import unittest
import stat
from pathlib import Path

from src.storage.artifact_files import LocalArtifactFileStore


class LocalArtifactFileStoreTest(unittest.TestCase):
    def test_save_open_and_delete_uses_opaque_storage_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "source.txt"
            source.write_text("hello", encoding="utf-8")
            store = LocalArtifactFileStore(root / "store")

            record = store.save_file(artifact_id="art-1", filename="report.txt", source_path=source)

            self.assertEqual(record.storage_key, "art-1/report.txt")
            self.assertEqual(record.size_bytes, 5)
            self.assertEqual(store.open_path(record.storage_key).read_text(encoding="utf-8"), "hello")
            self.assertTrue(store.delete(record.storage_key))
            self.assertFalse(store.open_path(record.storage_key).exists())

    def test_exact_retry_is_idempotent_and_conflicting_retry_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "source.txt"
            source.write_text("hello", encoding="utf-8")
            store = LocalArtifactFileStore(root / "store")

            first = store.save_file(
                artifact_id="art-1",
                filename="report.txt",
                source_path=source,
            )
            retried = store.save_file(
                artifact_id="art-1",
                filename="report.txt",
                source_path=source,
            )
            self.assertEqual(retried, first)
            self.assertEqual(
                stat.S_IMODE(store.open_path(first.storage_key).stat().st_mode),
                0o600,
            )

            source.write_text("changed", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                store.save_file(
                    artifact_id="art-1",
                    filename="report.txt",
                    source_path=source,
                )

    def test_rejects_storage_key_path_escape(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = LocalArtifactFileStore(Path(tmpdir) / "store")
            with self.assertRaises(ValueError):
                store.open_path("../secret.txt")


if __name__ == "__main__":
    unittest.main()
