from __future__ import annotations

import ast
import inspect
import unittest
from pathlib import Path

from src.api import runtime


class ApiPersistencePortAdoptionTest(unittest.TestCase):
    def test_private_runtime_helpers_use_narrow_persistence_ports(self) -> None:
        self.assertEqual(
            inspect.signature(runtime._mark_remote_continuation_dispatched)
            .parameters["storage"]
            .annotation,
            "MCPRemoteTaskStoragePort",
        )
        self.assertEqual(
            inspect.signature(runtime._resolve_conversation_memory_builder)
            .parameters["storage"]
            .annotation,
            "ConversationMemoryStoragePort",
        )

    def test_public_runtime_storage_annotation_remains_aggregate_compat_seam(self) -> None:
        self.assertEqual(
            inspect.signature(runtime.ApiRuntime.__init__)
            .parameters["storage"]
            .annotation,
            "StoragePort",
        )
        root = Path(__file__).resolve().parents[2]
        tree = ast.parse(
            (root / "src/api/runtime.py").read_text(encoding="utf-8")
        )
        aggregate_annotations = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.arg)
            and isinstance(node.annotation, ast.Name)
            and node.annotation.id == "StoragePort"
        ]
        self.assertEqual(len(aggregate_annotations), 1)


if __name__ == "__main__":
    unittest.main()
