from __future__ import annotations

import unittest

import _bootstrap  # noqa: F401
from sql_query_skill.platform_handler import SQLQueryPlatformHandler, build_handler


class SQLQueryPlatformHandlerTest(unittest.TestCase):
    def test_build_handler_returns_platform_handler(self) -> None:
        handler = build_handler()

        self.assertIsInstance(handler, SQLQueryPlatformHandler)

    def test_skill_manifest_uses_generic_non_stream_llm_service(self) -> None:
        manifest = _bootstrap.SKILL_ROOT.joinpath("SKILL.md").read_text(encoding="utf-8")

        self.assertIn("handler_module: runtime/sql_query_skill/platform_handler.py", manifest)
        self.assertIn("handler_factory: build_handler", manifest)
        self.assertIn("- llm.non_stream", manifest)
        self.assertNotIn("llm." + "sql_query", manifest)


if __name__ == "__main__":
    unittest.main()
