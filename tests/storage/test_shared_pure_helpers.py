from __future__ import annotations

import unittest

from src.state.postgres import schema_reconciler
from src.storage import artifact_files, conversation_files
from src.storage.path_safety import sanitize_download_filename
from src.storage.postgres import bootstrap
from src.storage.sql_text import split_sql_script


class SharedPureHelpersTest(unittest.TestCase):
    def test_filename_consumers_share_exact_sanitizer(self) -> None:
        self.assertIs(
            artifact_files.sanitize_download_filename,
            sanitize_download_filename,
        )
        self.assertIs(
            conversation_files.sanitize_download_filename,
            sanitize_download_filename,
        )
        cases = {
            r"C:\temp\report.csv": "report.csv",
            "/tmp/report.csv": "report.csv",
            " bad\x00name . ": "bad_name",
            "..": "download.bin",
            "": "download.bin",
            "a" * 201: "a" * 200,
        }
        for value, expected in cases.items():
            self.assertEqual(sanitize_download_filename(value), expected, value)

    def test_postgres_consumers_share_exact_sql_splitter(self) -> None:
        self.assertIs(bootstrap._split_sql, split_sql_script)
        self.assertIs(schema_reconciler._split_sql, split_sql_script)
        script = """\
CREATE TABLE first (id INTEGER);
DO $$
BEGIN
  PERFORM 1;
END
$$;
CREATE INDEX idx_first ON first (id);
SELECT 1
"""
        self.assertEqual(
            split_sql_script(script),
            [
                "CREATE TABLE first (id INTEGER);",
                "DO $$\nBEGIN\n  PERFORM 1;\nEND\n$$;",
                "CREATE INDEX idx_first ON first (id);",
                "SELECT 1",
            ],
        )


if __name__ == "__main__":
    unittest.main()
