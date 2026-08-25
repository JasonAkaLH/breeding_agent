from __future__ import annotations

import ast
import inspect
import unittest
from pathlib import Path

from src.api import runtime
from src.auth.services import UsernameTokenService


class ApiPersistencePortAdoptionTest(unittest.TestCase):
    def test_auth_service_uses_narrow_auth_storage_port(self) -> None:
        self.assertEqual(
            inspect.signature(UsernameTokenService.__init__)
            .parameters["storage"]
            .annotation,
            "AuthStoragePort",
        )
        root = Path(__file__).resolve().parents[2]
        tree = ast.parse(
            (root / "src/auth/services.py").read_text(encoding="utf-8")
        )
        contract_imports = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and node.module == "src.core.contracts"
            for alias in node.names
        }
        self.assertIn("AuthStoragePort", contract_imports)
        self.assertNotIn("StoragePort", contract_imports)

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
