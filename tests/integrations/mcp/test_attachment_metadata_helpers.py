from __future__ import annotations

import unittest

from src.integrations.mcp import dispatch_coordinator, selector_context
from src.integrations.mcp._attachment_metadata import (
    safe_attachment_basename,
    safe_attachment_content_type,
    truncate_utf8,
)


class AttachmentMetadataHelpersTest(unittest.TestCase):
    def test_coordinator_and_selector_share_attachment_helper_identity(self) -> None:
        self.assertIs(
            dispatch_coordinator._safe_attachment_basename,
            safe_attachment_basename,
        )
        self.assertIs(
            selector_context._safe_attachment_basename,
            safe_attachment_basename,
        )
        self.assertIs(
            dispatch_coordinator._safe_attachment_content_type,
            safe_attachment_content_type,
        )
        self.assertIs(
            selector_context._safe_attachment_content_type,
            safe_attachment_content_type,
        )

    def test_basename_preserves_existing_sanitization_and_byte_limit(self) -> None:
        self.assertEqual(safe_attachment_basename(None), "attachment")
        self.assertEqual(safe_attachment_basename("a/b\\c.csv"), "c.csv")
        self.assertEqual(safe_attachment_basename("\x00\x7fname.txt"), "name.txt")
        bounded = safe_attachment_basename("稻" * 100)
        self.assertLessEqual(len(bounded.encode("utf-8")), 255)
        self.assertEqual(bounded, "稻" * 85)

    def test_content_type_and_truncate_preserve_exact_boundaries(self) -> None:
        self.assertEqual(
            safe_attachment_content_type(" text/csv "),
            "text/csv",
        )
        for value in (None, "", "text/\x00csv", "x" * 256):
            self.assertEqual(
                safe_attachment_content_type(value),
                "application/octet-stream",
            )
        self.assertEqual(truncate_utf8("abc", 3), "abc")
        self.assertEqual(truncate_utf8("稻a", 3), "稻")
        self.assertEqual(truncate_utf8("稻a", 2), "")


if __name__ == "__main__":
    unittest.main()
