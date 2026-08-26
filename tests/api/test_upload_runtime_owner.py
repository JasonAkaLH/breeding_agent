from __future__ import annotations

import ast
import inspect
import unittest
from pathlib import Path

from src.api.file_selection_runtime import ConversationFileSelectionRuntimeMixin
from src.api.runtime import ApiRuntime
from src.api.upload_runtime import ConversationUploadRuntimeMixin


UPLOAD_RUNTIME_METHODS = {
    "_read_conversation_file_resource_bytes_exact",
    "_resolve_uploads",
    "_conversation_file_context_metadata_for_task",
    "_normalize_upload_sheet_selections",
    "_open_sheet_selection_interrupt",
    "_raise_missing_uploads",
    "_sheet_selection_question",
    "_upload_context_metadata",
    "delete_upload",
    "ensure_upload_allowed",
    "list_uploads",
    "resolve_conversation_uploads_for_message",
    "resolve_conversation_uploads_for_submission",
    "resolve_uploads_for_message",
    "resolve_uploads_for_submission",
    "save_upload",
}


class UploadRuntimeOwnerTest(unittest.TestCase):
    def test_upload_runtime_has_one_exact_mixin_owner(self) -> None:
        self.assertEqual(
            ApiRuntime.__mro__[:3],
            (
                ApiRuntime,
                ConversationFileSelectionRuntimeMixin,
                ConversationUploadRuntimeMixin,
            ),
        )
        self.assertEqual(
            ConversationUploadRuntimeMixin.__module__,
            "src.api.upload_runtime",
        )
        direct_methods = {
            name
            for name, value in inspect.getmembers(
                ConversationUploadRuntimeMixin,
                predicate=inspect.isfunction,
            )
            if name != "__subclasshook__"
        }
        self.assertEqual(direct_methods, UPLOAD_RUNTIME_METHODS)
        for name in UPLOAD_RUNTIME_METHODS:
            self.assertIs(
                getattr(ApiRuntime, name),
                getattr(ConversationUploadRuntimeMixin, name),
                name,
            )

    def test_api_runtime_does_not_redeclare_upload_methods(self) -> None:
        root = Path(__file__).resolve().parents[2]
        tree = ast.parse(
            (root / "src/api/runtime.py").read_text(encoding="utf-8")
        )
        runtime_class = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "ApiRuntime"
        )
        direct_methods = {
            node.name
            for node in runtime_class.body
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        }
        self.assertTrue(UPLOAD_RUNTIME_METHODS.isdisjoint(direct_methods))


if __name__ == "__main__":
    unittest.main()
