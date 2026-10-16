from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from src.storage.knowledge_files import KnowledgeFileStore


class KnowledgeFilesTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = KnowledgeFileStore(Path(self.directory.name) / "knowledge")

    def tearDown(self):
        self.directory.cleanup()

    def test_original_bytes_private_permissions_and_cross_library_independence(self):
        raw = b"\xef\xbb\xbf# Title\r\nBody \r\n"
        first = self.store.put_body("one", "import1", raw)
        second = self.store.put_body("two", "import2", raw)
        self.assertEqual(first["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(self.store.read_body(first["body_ref"]), raw)
        self.assertEqual(
            os.stat(self.store.root_dir / first["body_ref"]).st_mode & 0o777, 0o600
        )
        self.assertEqual(os.stat(self.store.root_dir / "one").st_mode & 0o777, 0o700)
        self.store.delete_body(first["body_ref"])
        self.assertEqual(self.store.read_body(second["body_ref"]), raw)
        self.store.delete_body(first["body_ref"])
        self.assertEqual(self.store.put_body("two", "import2", raw), second)

    def test_path_symlink_hardlink_corruption_refused(self):
        for ref in ["../outside", "/etc/passwd", "one/../abc.md", "one/two/invalid.md"]:
            with self.assertRaises(ValueError):
                self.store.read_body(ref)
        result = self.store.put_body("one", "resource", b"plain")
        path = self.store.root_dir / result["body_ref"]
        path.write_bytes(b"corrupt")
        with self.assertRaises(ValueError):
            self.store.read_body(result["body_ref"])
        path.unlink()
        target = Path(self.directory.name) / "target"
        target.write_bytes(b"plain")
        path.symlink_to(target)
        with self.assertRaises(OSError):
            self.store.read_body(result["body_ref"])
        with self.assertRaises(ValueError):
            self.store.delete_body(result["body_ref"])
        path.unlink()
        result = self.store.put_body("one", "hardlink", b"plain")
        path = self.store.root_dir / result["body_ref"]
        path.unlink()
        os.link(target, path)
        with self.assertRaises(ValueError):
            self.store.read_body(result["body_ref"])
        directory = self.store.root_dir / "external"
        directory.symlink_to(target.parent, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.store.put_body("external", "one", b"plain")

    def test_cleanup_tombstone_prevents_late_upload_and_removes_crash_temp(self):
        raw = b"late upload"
        ref = self.store.build_reference(
            "library", "import1", hashlib.sha256(raw).hexdigest()
        )
        self.store.delete_body(ref)
        with self.assertRaises(ValueError):
            self.store.put_body("library", "import1", raw)
        other = self.store.put_body("library", "import2", raw)
        path = self.store.root_dir / other["body_ref"]
        temporary = path.parent / ".write-crashed"
        temporary.write_bytes(b"partial raw body")
        temporary.chmod(0o600)
        self.store.delete_body(other["body_ref"])
        self.assertFalse(temporary.exists())
        self.store.delete_library_files("library")
        with self.assertRaises(ValueError):
            self.store.put_body("library", "new_import", raw)
        with self.assertRaises(ValueError):
            self.store.publish_index("library", 2, "late index")
        self.assertEqual(
            self.store.put_body("other", "new_import", raw)["sha256"],
            hashlib.sha256(raw).hexdigest(),
        )

    def test_index_atomic_monotonic_under_concurrent_publish(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(
                pool.map(
                    lambda n: self.store.publish_index("library", n, f"# Revision {n}"),
                    [3, 1, 4, 2],
                )
            )
        self.assertEqual(self.store.read_index("library"), (4, "# Revision 4"))
        self.assertFalse(self.store.publish_index("library", 1, "old"))
        self.assertTrue(self.store.publish_index("library", 4, "# Revision 4"))
        with self.assertRaises(ValueError):
            self.store.publish_index("library", 4, "different")
        self.assertEqual(list((self.store.root_dir / "library").glob(".index-*")), [])

    def test_delete_library_removes_only_managed_library(self):
        first = self.store.put_body("one", "first", b"one")
        second = self.store.put_body("two", "second", b"two")
        self.store.publish_index("one", 1, "# One")
        self.store.delete_library_files("one")
        with self.assertRaises(FileNotFoundError):
            self.store.read_body(first["body_ref"])
        self.assertEqual(self.store.read_body(second["body_ref"]), b"two")
        self.store.delete_library_files("one")
