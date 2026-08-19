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

    def test_read_utf8_verifies_size_digest_and_encoding(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "source.json"
            source.write_text('{"text":"原始返回"}', encoding="utf-8")
            store = LocalArtifactFileStore(root / "store")
            record = store.save_file(
                artifact_id="art-text",
                filename="result.json",
                source_path=source,
            )

            self.assertEqual(
                store.read_utf8(
                    record.storage_key,
                    expected_size_bytes=record.size_bytes,
                    expected_sha256=record.sha256,
                ),
                '{"text":"原始返回"}',
            )
            with self.assertRaises(ValueError):
                store.read_utf8(
                    record.storage_key,
                    expected_size_bytes=record.size_bytes + 1,
                    expected_sha256=record.sha256,
                )
            with self.assertRaises(ValueError):
                store.read_utf8(
                    record.storage_key,
                    expected_size_bytes=record.size_bytes,
                    expected_sha256="0" * 64,
                )

            binary_source = root / "binary.bin"
            binary_source.write_bytes(b"\xff")
            binary = store.save_file(
                artifact_id="art-binary",
                filename="result.json",
                source_path=binary_source,
            )
            with self.assertRaises(ValueError):
                store.read_utf8(
                    binary.storage_key,
                    expected_size_bytes=binary.size_bytes,
                    expected_sha256=binary.sha256,
                )


if __name__ == "__main__":
    unittest.main()
